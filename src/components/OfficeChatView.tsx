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
  appendHintMessage,
  appendUserMessage,
  createConversation,
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
  appendConversationResult,
  getArtifactTarget,
  setArtifactTarget,
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

import { runConversationTurn } from '@/services/conversationActions'
import { ConversationArtifact } from '@/components/ConversationArtifact'
import { ImagePreviewOverlay } from '@/components/ImagePreviewOverlay'

function hexColor(color: number): string {
  return `#${color.toString(16).padStart(6, '0')}`
}

/** 图片附件在附件条上直接渲染缩略图，直观看到待发送内容 */
function uploadThumbUrl(id: string): string {
  return `${OFFICE_API_BASE}/api/uploads/${id}/raw`
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
  const [previewImage, setPreviewImage] = useState<{ src: string; alt: string } | null>(null)
  const target = useSyncExternalStore(subscribeChat, () => getArtifactTarget(roster))
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

  /** 添加图片：>2MB 直接提示并跳过（后端视觉输入上限），合计最多 4 张 */
  async function addImages(files: File[]) {
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

  async function handlePickImages(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? [])
    event.target.value = '' // 允许重复选同一文件
    await addImages(files)
  }

  /** 粘贴图片：截图/复制的图片直接入列，无需先存盘再选择 */
  function handlePaste(event: React.ClipboardEvent<HTMLInputElement>) {
    const pasted = Array.from(event.clipboardData?.files ?? []).filter((file) =>
      file.type.startsWith('image/'),
    )
    if (pasted.length === 0) return // 纯文本粘贴走默认行为
    event.preventDefault()
    const stamp = Date.now()
    const renamed = pasted.map((file, index) => {
      const ext = file.type.split('/')[1]?.replace('jpeg', 'jpg') || 'png'
      return new File([file], `粘贴图片-${stamp}${pasted.length > 1 ? `-${index + 1}` : ''}.${ext}`, { type: file.type })
    })
    void addImages(renamed)
  }

  function handleRemoveImage(id: string) {
    setImages((prev) => prev.filter((item) => item.id !== id))
    void removeUpload(id)
  }

  async function handleSend() {
    const text = input.trim()
    if ((!text && images.length === 0) || pending) return
    const message = text || '请查看这张图片'
    const controller = beginChat(roster)
    if (!controller) return

    const previousMessages = getThreadSnapshot(roster)
    const history = previousMessages.filter((m) => !m.hint).map((m) => ({
      role: m.role,
      content: m.content,
    }))
    const uploadIds = images.map((item) => item.id)
    appendUserMessage(roster, message, uploadIds)
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
    let executingAction = true
    try {
      const result = await runConversationTurn({ thread: roster, message, history: previousMessages, uploadIds, signal: controller.signal })
      executingAction = false
      if (result.handled) {
        appendConversationResult(roster, result.reply || '本轮操作已完成', result.artifacts)
        if (result.artifacts?.length) setArtifactTarget(roster, '')
        return
      }
      if (result.context) history.push({ role: 'assistant', content: result.context })
      const res = await fetch(`${OFFICE_API_BASE}/api/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          roster,
          message,
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
        if (executingAction) {
          appendHintMessage(roster, '已停止等待；已提交的产物操作可能仍在后台执行，完成后可继续询问结果。')
          return
        }
        updateLastAssistantMessage(
          roster,
          acc ? `${acc}\n\n（已手动中断）` : '（已手动中断）',
          degraded ? 'mock' : 'llm',
          model,
        )
        showToast('已中断本次回复')
      } else if (!acc) {
        appendHintMessage(roster, `本轮未完成：${(error as Error).message || '无法连接后端'}。可重新发送。`)
        showToast((error as Error).message || '发送失败')
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

  return (
    <div className="view-page chat-page">
      <header className="view-head">
        <div>
          <h1>单飞</h1>
          <p>向任一角色直接提需求，完成后继续聊天修改；每个角色独立保留上下文和产物</p>
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
                createConversation(roster)
                showToast('已开启新对话，历史对话在左侧菜单栏')
              }}
            >
              ＋ 新建对话
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
                  <ChatBubble key={i} message={message} roster={roster} onPreview={(src) => setPreviewImage({ src, alt: '附件图片' })} />
                ),
              )
            )}
            {pending ? (
              <p className="chat-typing">{displayNameOf(roster)} 正在处理…</p>
            ) : null}
          </div>

          {images.length > 0 ? (
            <ul className="chat-image-chips">
              {images.map((item) => (
                <li key={item.id} className="chat-image-chip has-thumb">
                  <img
                    className="attachment-thumb"
                    src={uploadThumbUrl(item.id)}
                    alt={item.name}
                    title="点击放大查看"
                    onClick={() => setPreviewImage({ src: uploadThumbUrl(item.id), alt: item.name })}
                  />
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

          {target ? <p className="chat-hint">正在基于 {target} 修改 <button type="button" onClick={() => setArtifactTarget(roster, '')}>取消指定</button></p> : null}
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
              placeholder={`给 ${displayNameOf(roster)} 发消息…（可直接粘贴截图）`}
              onPaste={handlePaste}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleSend()
              }}
            />
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
              disabled={pending || (!input.trim() && images.length === 0)}
              onClick={handleSend}
            >
              发送
            </button>
          </footer>
        </section>
      </div>

      {previewImage ? (
        <ImagePreviewOverlay
          src={previewImage.src}
          alt={previewImage.alt}
          onClose={() => setPreviewImage(null)}
        />
      ) : null}
    </div>
  )
}

function ChatBubble({ message, roster, onPreview }: { message: ChatMessage; roster: number; onPreview?: (src: string) => void }) {
  const isUser = message.role === 'user'
  return (
    <div className={`chat-bubble-row ${isUser ? 'mine' : ''}`}>
      <div className="chat-bubble">
        <p>{message.content}</p>
        {message.images?.length ? (
          <span className="talk-msg-images">
            {message.images.map(id => (
              <img key={id} src={uploadThumbUrl(id)} alt="附件图片"
                onClick={() => onPreview?.(uploadThumbUrl(id))} />
            ))}
          </span>
        ) : null}
        {message.artifacts?.map(artifact => <ConversationArtifact key={artifact.id} artifact={artifact} thread={roster} />)}
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
