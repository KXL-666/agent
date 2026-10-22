import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ChangeEvent,
} from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { setAgentState } from '@/scene/officeSceneBridge'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'
import {
  appendAssistantMessage,
  appendHintMessage,
  appendUserMessage,
  clearThread,
  getSelectedRoster,
  getThreadSnapshot,
  setSelectedRoster,
  subscribeChat,
  updateLastAssistantMessage,
  useRestoreChat,
  type ChatMessage,
} from '@/store/officeChatStore'
import {
  ensureAgentConfigsLoaded,
  getAgentConfigsSnapshot,
  subscribeAgentConfigs,
} from '@/store/officeAgentConfigStore'
import { getOfficeAgents } from '@/store/officeStore'
import { showToast } from '@/store/officeUiStore'
import { openSprintDialog } from '@/store/officeViewStore'
import {
  removeUpload,
  uploadFiles,
  type OfficeUpload,
} from '@/store/officeUploadStore'

function hexColor(color: number): string {
  return `#${color.toString(16).padStart(6, '0')}`
}

/** 后端视觉输入上限 2MB / 单条消息最多 4 张图片 */
const MAX_IMAGE_BYTES = 2 * 1024 * 1024
const MAX_IMAGES = 4

export function OfficeChatView() {
  const roster = useSyncExternalStore(subscribeChat, getSelectedRoster)
  const thread = useSyncExternalStore(subscribeChat, () =>
    getThreadSnapshot(roster),
  )
  const [input, setInput] = useState('')
  const [pending, setPending] = useState(false)
  const [images, setImages] = useState<OfficeUpload[]>([])
  const [uploading, setUploading] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const imageInputRef = useRef<HTMLInputElement>(null)

  useRestoreChat()

  const entry = AGENT_ROSTER[roster - 1]
  const agentConfigs = useSyncExternalStore(
    subscribeAgentConfigs,
    getAgentConfigsSnapshot,
  )
  // 自定义姓名优先于名册原名
  const displayNameOf = (rosterNo: number) =>
    agentConfigs[rosterNo]?.name || AGENT_ROSTER[rosterNo - 1]?.name || `#${rosterNo}`
  const avatarOf = (rosterNo: number) => agentConfigs[rosterNo]?.avatar || ''

  useEffect(() => {
    ensureAgentConfigsLoaded()
  }, [])

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [thread.length, roster])

  /** 选择图片：>2MB 直接提示并跳过（后端视觉输入上限），合计最多 4 张 */
  async function handlePickImages(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? [])
    event.target.value = '' // 允许重复选同一文件
    if (files.length === 0) return
    const room = MAX_IMAGES - images.length
    if (room <= 0) {
      showToast(`最多附带 ${MAX_IMAGES} 张图片`)
      return
    }
    const picked = files.slice(0, room)
    if (files.length > room) {
      showToast(`最多附带 ${MAX_IMAGES} 张图片，已忽略多余选择`)
    }
    const oversized: string[] = []
    const valid = picked.filter((file) => {
      if (file.size > MAX_IMAGE_BYTES) {
        oversized.push(file.name)
        return false
      }
      return true
    })
    if (oversized.length > 0) {
      showToast(`图片需≤2MB，已跳过：${oversized.join('、')}`)
    }
    if (valid.length === 0) return
    setUploading(true)
    try {
      const result = await uploadFiles(valid)
      setImages((prev) => [...prev, ...result.uploads])
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : '图片上传失败，请确认后端已启动',
      )
    } finally {
      setUploading(false)
    }
  }

  function handleRemoveImage(id: string) {
    setImages((prev) => prev.filter((item) => item.id !== id))
    void removeUpload(id)
  }

  async function handleSend() {
    const text = input.trim()
    if (!text || pending) return

    const history = getThreadSnapshot(roster).map((m) => ({
      role: m.role,
      content: m.content,
    }))
    const uploadIds = images.map((item) => item.id)
    appendUserMessage(roster, text)
    setInput('')
    setImages([])
    setPending(true)

    // 场景联动：该员工走到话筒前"回复老板"，4 秒后回到原状态
    const agent = getOfficeAgents()[roster - 1]
    const prevState = agent?.state ?? 'working'
    const prevTask = agent?.currentTask
    if (agent) setAgentState(agent.id, 'talking', '正在回复老板消息…')

    // 流式回复：SSE 逐帧增量渲染，可随时中断
    const controller = new AbortController()
    abortRef.current = controller
    let acc = ''
    let model: string | undefined
    let degraded = false
    let imagesDropped = false
    let backupUsed = false
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          roster,
          message: text,
          history,
          upload_ids: uploadIds,
        }),
        signal: controller.signal,
      })
      if (!res.ok || !res.body) throw new Error(String(res.status))
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let boundary = buffer.indexOf('\n\n')
        while (boundary >= 0) {
          const frame = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          boundary = buffer.indexOf('\n\n')
          const line = frame.split('\n').find((l) => l.startsWith('data:'))
          if (!line) continue
          try {
            const payload = JSON.parse(line.slice(5).trim()) as {
              start?: boolean
              model?: string
              delta?: string
              done?: boolean
              degraded?: boolean
              images_dropped?: boolean
              backup_used?: boolean
            }
            if (payload.start) {
              model = payload.model || undefined
            } else if (typeof payload.delta === 'string') {
              acc += payload.delta
              updateLastAssistantMessage(roster, acc, 'llm', model)
            } else if (payload.done) {
              degraded = Boolean(payload.degraded)
              imagesDropped = Boolean(payload.images_dropped)
              backupUsed = Boolean(payload.backup_used)
            }
          } catch {
            /* 忽略不完整帧 */
          }
        }
      }
      if (!acc) throw new Error('empty reply')
      // 收尾：兜底生成的内容按实际标注 Mock，避免误导
      updateLastAssistantMessage(roster, acc, degraded ? 'mock' : 'llm', model)
      if (imagesDropped) {
        appendHintMessage(roster, '该模型不支持图片识别，已忽略图片')
      }
      if (backupUsed) {
        appendHintMessage(roster, '已切换备用 API 应答')
      }
    } catch (error) {
      if ((error as Error).name === 'AbortError') {
        updateLastAssistantMessage(
          roster,
          acc ? `${acc}\n\n（已手动中断）` : '（已手动中断）',
          degraded ? 'mock' : 'llm',
          model,
        )
        showToast('已中断本次回复')
      } else if (!acc) {
        appendAssistantMessage(roster, '（连接失败，请确认后端已启动）', 'mock')
        showToast('发送失败：无法连接后端，请确认服务已启动')
      }
    } finally {
      setPending(false)
      abortRef.current = null
      if (agent) {
        setTimeout(
          () => setAgentState(agent.id, prevState, prevTask),
          4000,
        )
      }
    }
  }

  /** 把当前输入作为需求打开新建任务弹窗：可补齐附件、保存位置、极速模式与预算后再开工 */
  function handleStartSprint() {
    const text = input.trim()
    if (!text) return
    openSprintDialog(text)
    setInput('')
  }

  return (
    <div className="view-page chat-page">
      <header className="view-head">
        <div>
          <h1>对话中心</h1>
          <p>选择任意员工，以 TA 的角色身份与你对话（各成员使用自己配置的模型）</p>
        </div>
      </header>

      <div className="chat-layout">
        <aside className="chat-roster">
          {AGENT_ROSTER.map((item, index) => (
            <button
              key={item.id}
              type="button"
              className={`chat-roster-item ${roster === index + 1 ? 'active' : ''}`}
              onClick={() => setSelectedRoster(index + 1)}
            >
              <span
                className="agent-avatar small"
                style={{ background: hexColor(item.color) }}
              >
                {avatarOf(index + 1) ? (
                  <span className="agent-avatar-emoji">{avatarOf(index + 1)}</span>
                ) : (
                  displayNameOf(index + 1).slice(0, 1)
                )}
              </span>
              <span>
                <strong>{displayNameOf(index + 1)}</strong>
                <em>{item.role}</em>
              </span>
            </button>
          ))}
        </aside>

        <section className="chat-panel">
          <header className="chat-head">
            <span
              className="agent-avatar small"
              style={{ background: hexColor(entry?.color ?? 0x888888) }}
            >
              {avatarOf(roster) ? (
                <span className="agent-avatar-emoji">{avatarOf(roster)}</span>
              ) : (
                displayNameOf(roster).slice(0, 1) ?? '?'
              )}
            </span>
            <div>
              <strong>{displayNameOf(roster)}</strong>
              <em>{entry?.role ?? ''}</em>
            </div>
            <button
              type="button"
              className="view-ghost-btn"
              onClick={() => {
                clearThread(roster)
                showToast('聊天记录已清空')
              }}
            >
              清空记录
            </button>
          </header>

          <div className="chat-messages" ref={listRef}>
            {thread.length === 0 ? (
              <p className="chat-empty">
                和 {displayNameOf(roster)} 说点什么吧，比如「最近工作进展如何？」
              </p>
            ) : (
              thread.map((message, i) =>
                message.hint ? (
                  <p key={i} className="chat-hint">
                    {message.content}
                  </p>
                ) : (
                  <ChatBubble key={i} message={message} />
                ),
              )
            )}
            {pending ? (
              <p className="chat-typing">{displayNameOf(roster)} 正在输入…</p>
            ) : null}
          </div>

          {images.length > 0 ? (
            <ul className="chat-image-chips">
              {images.map((item) => (
                <li key={item.id} className="chat-image-chip">
                  <span className="chat-image-name" title={item.name}>
                    {item.name}
                  </span>
                  <button
                    type="button"
                    aria-label="移除图片"
                    onClick={() => handleRemoveImage(item.id)}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          ) : null}

          <footer className="chat-input-row">
            <input
              ref={imageInputRef}
              type="file"
              accept="image/*"
              multiple
              hidden
              onChange={handlePickImages}
            />
            <button
              type="button"
              className="view-ghost-btn chat-attach-btn"
              disabled={pending || uploading}
              title="添加图片附件（最多 4 张，每张 ≤2MB）"
              onClick={() => imageInputRef.current?.click()}
            >
              {uploading ? '…' : '📎'}
            </button>
            <input
              value={input}
              placeholder={`给 ${displayNameOf(roster)} 发消息…`}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleSend()
              }}
            />
            <button
              type="button"
              className="view-ghost-btn"
              disabled={pending || !input.trim()}
              onClick={handleStartSprint}
              title="打开新建任务弹窗，可补齐附件/保存位置/极速模式/预算后开工"
            >
              交给团队开工
            </button>
            {pending ? (
              <button
                type="button"
                className="view-ghost-btn"
                onClick={() => abortRef.current?.abort()}
              >
                中断
              </button>
            ) : null}
            <button
              type="button"
              className="view-primary-btn"
              disabled={pending || !input.trim()}
              onClick={handleSend}
            >
              发送
            </button>
          </footer>
        </section>
      </div>
    </div>
  )
}

function ChatBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user'
  return (
    <div className={`chat-bubble-row ${isUser ? 'mine' : ''}`}>
      <div className="chat-bubble">
        <p>{message.content}</p>
        <span className="chat-meta">
          {message.time}
          {!isUser && message.via ? (
            <i className={`chat-via ${message.via}`}>
              {message.via === 'llm' ? (message.model ?? 'LLM') : 'Mock'}
            </i>
          ) : null}
        </span>
      </div>
    </div>
  )
}
