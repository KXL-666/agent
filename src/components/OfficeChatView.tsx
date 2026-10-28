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
  appendTaskMessage,
  appendUserMessage,
  clearThread,
  getSelectedRoster,
  getThreadSnapshot,
  setSelectedRoster,
  subscribeChat,
  updateLastAssistantMessage,
  useRestoreChat,
  beginChat,
  finishChat,
  abortChat,
  getPendingRosters,
  type ChatMessage,
} from '@/store/officeChatStore'
import {
  ensureAgentConfigsLoaded,
  getAgentConfigsSnapshot,
  subscribeAgentConfigs,
} from '@/store/officeAgentConfigStore'
import { getOfficeAgents } from '@/store/officeStore'
import { showToast } from '@/store/officeUiStore'
import { setActiveView } from '@/store/officeViewStore'
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
  const [drafts, setDrafts] = useState<Record<number, string>>({})
  const input = drafts[roster] ?? ''
  const setInput = (value: string) => setDrafts((prev) => ({ ...prev, [roster]: value }))
  const pendingRosters = useSyncExternalStore(subscribeChat, getPendingRosters)
  const pending = pendingRosters.includes(roster)
  const [imageThreads, setImageThreads] = useState<Record<number, OfficeUpload[]>>({})
  const images = imageThreads[roster] ?? []
  const setImages = (value: OfficeUpload[] | ((previous: OfficeUpload[]) => OfficeUpload[])) =>
    setImageThreads((prev) => ({ ...prev, [roster]: typeof value === 'function' ? value(prev[roster] ?? []) : value }))
  const [uploading, setUploading] = useState(false)
  const [assigning, setAssigning] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)
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
  }, [thread, roster])

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
    const controller = beginChat(roster)
    if (!controller) return

    const history = getThreadSnapshot(roster).filter((m) => !m.hint).map((m) => ({
      role: m.role,
      content: m.content,
    }))
    const uploadIds = images.map((item) => item.id)
    appendUserMessage(roster, text)
    setInput('')
    setImages([])

    // 场景联动：该员工走到话筒前"回复老板"，4 秒后回到原状态
    const agent = getOfficeAgents()[roster - 1]
    const prevState = agent?.state ?? 'working'
    const prevTask = agent?.currentTask
    if (agent) setAgentState(agent.id, 'talking', '正在回复老板消息…')

    // 流式回复：SSE 逐帧增量渲染，可随时中断
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
      } else {
        appendHintMessage(roster, '连接已断开，以上为部分回复，可继续追问。')
      }
    } finally {
      finishChat(roster)
      if (agent) {
        setTimeout(
          () => setAgentState(agent.id, prevState, prevTask),
          4000,
        )
      }
    }
  }

  /** 把当前输入作为任务派给当前成员：单兵作战，不触发团队 Sprint */
  async function handleAssignTask() {
    const text = input.trim()
    if (!text || assigning) return
    setAssigning(true)
    appendUserMessage(roster, `📌 指派任务：${text}`)
    setInput('')
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/solo/task`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ roster, message: text }),
      })
      const payload = (await res.json().catch(() => null)) as
        | { task_id?: string; detail?: string }
        | null
      if (!res.ok || !payload?.task_id) {
        throw new Error(payload?.detail ?? `请求失败（${res.status}）`)
      }
      appendTaskMessage(roster, text.slice(0, 30), payload.task_id)
      showToast(`已指派给 ${displayNameOf(roster)}，TA 开始单兵作战`)
    } catch (error) {
      appendHintMessage(
        roster,
        `任务指派失败：${(error as Error).message || '无法连接后端'}`,
      )
      showToast((error as Error).message || '任务指派失败')
    } finally {
      setAssigning(false)
    }
  }

  return (
    <div className="view-page chat-page">
      <header className="view-head">
        <div>
          <h1>单飞</h1>
          <p>各成员独立对话、独立干活：点「指派任务」让当前成员单兵作战，产物进「交付」面板</p>
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
                <em>{pendingRosters.includes(index + 1) ? '正在回复…' : item.role}</em>
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
              disabled={pending}
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
                ) : message.role === 'task' ? (
                  <TaskCard key={message.taskId ?? i} message={message} />
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
              disabled={pending || assigning || !input.trim()}
              onClick={handleAssignTask}
              title="把当前输入作为任务派给当前成员，TA 独立完成并产出到「交付」面板"
            >
              {assigning ? '指派中…' : '指派任务'}
            </button>
            {pending ? (
              <button
                type="button"
                className="view-ghost-btn"
                onClick={() => abortChat(roster)}
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

/** 单飞任务卡片：进行中 → 已完成/失败，完成后可跳转交付面板查看产物 */
function TaskCard({ message }: { message: ChatMessage }) {
  const status = message.taskStatus ?? 'running'
  const statusLabel =
    status === 'running' ? '执行中…' : status === 'done' ? '已完成' : '失败'
  return (
    <div className="chat-bubble-row">
      <div className={`chat-task-card is-${status}`}>
        <span className="chat-task-icon" aria-hidden>
          {status === 'running' ? '⏳' : status === 'done' ? '✅' : '❌'}
        </span>
        <div className="chat-task-body">
          <strong>{message.content}</strong>
          <span className="chat-task-status">
            {statusLabel} · {message.time}
          </span>
        </div>
        {status === 'done' && message.artifactId ? (
          <button
            type="button"
            className="view-ghost-btn chat-task-open"
            onClick={() => setActiveView('artifacts')}
          >
            查看产物
          </button>
        ) : null}
      </div>
    </div>
  )
}
