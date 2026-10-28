import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { useSprintPause } from '@/components/dashboard/officeComposer'
import { captureOfficeScreenshot } from '@/scene/officeSceneBridge'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'
import {
  isVoiceSupported,
  startVoiceRecognition,
} from '@/services/officeVoice'
import {
  ensureAgentConfigsLoaded,
  getAgentConfigsSnapshot,
  subscribeAgentConfigs,
} from '@/store/officeAgentConfigStore'
import {
  getArtifactsSnapshot,
  subscribeArtifacts,
  type OfficeArtifact,
} from '@/store/officeArtifactStore'
import {
  computeSprintProgress,
  displayTaskMeta,
  getActivitiesSnapshot,
  getSprintMetaSnapshot,
  getTasksSnapshot,
  subscribeSprintData,
  type SprintTask,
} from '@/store/officeTaskStore'
import { showToast } from '@/store/officeUiStore'
import {
  getViewedSprint,
  getViewedSprintMeta,
  setActiveView,
  subscribeView,
} from '@/store/officeViewStore'
import {
  getSprintSettingsSnapshot,
  startSprint,
} from '@/store/officeSprintSettings'
import {
  formatSize,
  removeUpload,
  uploadFiles,
  type OfficeUpload,
} from '@/store/officeUploadStore'
import { requestNotifyPermission } from '@/services/officeNotify'

import { runConversationTurn } from '@/services/conversationActions'
import { ConversationArtifact } from '@/components/ConversationArtifact'
import { appendUserMessage, appendConversationResult, getThreadSnapshot, subscribeChat, updateLastAssistantMessage, getArtifactTarget, setArtifactTarget, beginChat, finishChat, getPendingRosters } from '@/store/officeChatStore'

function hexColor(color: number): string {
  return `#${color.toString(16).padStart(6, '0')}`
}

const QUICK_TOOLS = [
  { icon: '📎', label: '添加文件' },
  { icon: '📁', label: '添加文件夹' },
  { icon: '□', label: '文件浏览' },
  { icon: '◎', label: '打开浏览器' },
  { icon: '⌗', label: '截图' },
] as const

function downloadDataUrl(dataUrl: string, filename: string) {
  const anchor = document.createElement('a')
  anchor.href = dataUrl
  anchor.download = filename
  anchor.click()
}

/** 对话指挥中心：按模板重设计——头部 + 消息流（调度记录/交接卡/产物卡）+ 底部输入区 */
export function TalkView() {
  const { running, paused, busy, togglePause } = useSprintPause()
  const tasks = useSyncExternalStore(subscribeSprintData, getTasksSnapshot)
  const activities = useSyncExternalStore(subscribeSprintData, getActivitiesSnapshot)
  const meta = useSyncExternalStore(subscribeSprintData, getSprintMetaSnapshot)
  const viewedMeta = useSyncExternalStore(subscribeView, getViewedSprintMeta)
  const viewedSprintId = useSyncExternalStore(subscribeView, getViewedSprint)
  const artifacts = useSyncExternalStore(subscribeArtifacts, getArtifactsSnapshot)
  const agentConfigs = useSyncExternalStore(subscribeAgentConfigs, getAgentConfigsSnapshot)

  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  /** 唯一团队对话线程；与六个单飞线程分别持久化。 */
  const chatMessages = useSyncExternalStore(subscribeChat, () => getThreadSnapshot(0))
  const target = useSyncExternalStore(subscribeChat, () => getArtifactTarget(0))
  const pendingRosters = useSyncExternalStore(subscribeChat, getPendingRosters)
  const [listening, setListening] = useState(false)
  const [attachments, setAttachments] = useState<OfficeUpload[]>([])
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const dirInputRef = useRef<HTMLInputElement | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [sheetOpen, setSheetOpen] = useState(false)
  const [manualTarget, setManualTarget] = useState<number | null>(null)
  const [thinkOpen, setThinkOpen] = useState(true)
  const listRef = useRef<HTMLDivElement>(null)
  const toolsRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    ensureAgentConfigsLoaded()
  }, [])

  const sprintKey = viewedSprintId || meta.id

  // 点击「＋」菜单以外的任意位置关闭菜单
  useEffect(() => {
    if (!menuOpen) return
    const close = (event: PointerEvent) => {
      const target = event.target as Node
      if (!toolsRef.current?.contains(target)) setMenuOpen(false)
    }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [menuOpen])

  const requirement = (viewedMeta?.requirement || meta.requirement).split('\n\n此前对话背景：')[0]
  const sprintStatus = viewedMeta?.status ?? meta.status
  const stage = viewedMeta?.stage ?? meta.stage
  const cost = viewedMeta?.cost ?? meta.cost
  const hasSprint = Boolean(requirement)
  useEffect(() => {
    if (running || !hasSprint || !sprintKey) return
    const delivered = artifacts.filter(a => a.sprint_id === sprintKey && (a.kind === 'final' || a.kind === 'custom'))
    const recorded = new Set(getThreadSnapshot(0).flatMap(m => m.artifacts?.map(a => a.id) ?? []))
    const fresh = delivered.filter(a => !recorded.has(a.id))
    if (fresh.length) appendConversationResult(0, '本轮产物已保存，可以直接继续提出修改要求。', fresh)
  }, [running, hasSprint, sprintKey, artifacts])
  const { done, total } = computeSprintProgress(tasks)

  const displayNameOf = (rosterNo: number) =>
    agentConfigs[rosterNo]?.name || AGENT_ROSTER[rosterNo - 1]?.name || `#${rosterNo}`
  const avatarOf = (rosterNo: number) => agentConfigs[rosterNo]?.avatar || ''
  const roleOf = (rosterNo: number) => AGENT_ROSTER[rosterNo - 1]?.role ?? ''

  // 消息流更新时滚到底部
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [tasks, artifacts, activities, requirement, sprintStatus, chatMessages])

  function handleQuickTool(label: string) {
    switch (label) {
      case '添加文件':
        fileInputRef.current?.click()
        return
      case '添加文件夹':
        dirInputRef.current?.click()
        return
      case '文件浏览':
        setActiveView('files')
        return
      case '打开浏览器':
        window.open(`${OFFICE_API_BASE}/docs`, '_blank', 'noopener')
        showToast('已在新标签页打开后端 API 文档')
        return
      case '截图': {
        const dataUrl = captureOfficeScreenshot()
        if (!dataUrl) {
          showToast('办公室画布尚未就绪，稍后再试')
          return
        }
        downloadDataUrl(dataUrl, `office-snapshot-${Date.now()}.png`)
        showToast('办公室画布截图已导出（PNG）')
        return
      }
    }
  }

  /** 附件选择：上传到后端暂存，发送时随需求一起提交 */
  async function handlePickAttachments(event: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? [])
    event.target.value = '' // 允许重复选同一文件
    if (files.length === 0) return
    setUploading(true)
    try {
      const result = await uploadFiles(files)
      setAttachments((prev) => [...prev, ...result.uploads])
      if (result.skipped.length > 0) {
        showToast(`以下文件超过 50MB 未上传：${result.skipped.join('、')}（请压缩或拆分后重试）`)
      }
    } catch (error) {
      showToast(error instanceof Error ? error.message : '附件上传失败，请确认后端已启动')
    } finally {
      setUploading(false)
    }
  }

  async function handleRemoveAttachment(id: string) {
    setAttachments((prev) => prev.filter((item) => item.id !== id))
    await removeUpload(id)
  }

  /** 语音输入：识别结果追加到输入框 */
  function handleVoice() {
    if (!isVoiceSupported()) {
      showToast('当前浏览器不支持语音识别，请改用 Chrome/Edge')
      return
    }
    if (listening) return
    showToast('正在聆听…')
    setListening(true)
    startVoiceRecognition()
      .then((voice) => {
        setText((prev) => (prev ? prev + voice : voice))
        showToast('语音已转成文字，确认后可发送')
      })
      .catch((error: Error) => showToast(error.message))
      .finally(() => setListening(false))
  }

  function handleStop() {
    if (!running) {
      showToast('当前没有进行中的任务')
      return
    }
    fetch(`${OFFICE_API_BASE}/api/sprint/stop`, { method: 'POST' })
      .then(async (res) => {
        if (!res.ok) {
          const data = await res.json().catch(() => ({}))
          showToast(data.detail || '结束失败')
        }
      })
      .catch(() => showToast('无法连接后端'))
  }

  /** Sprint 运行中向团队补充指示（后端广播 instruction_added，全员可见） */
  async function sendInstruct(payload: string): Promise<boolean> {
    setSending(true)
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/sprint/instruct`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: payload }),
      })
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { detail?: string }
        window.alert(data.detail || '下达指示失败')
        return false
      }
      showToast('📌 指示已下达，团队会结合新指示继续工作')
      return true
    } catch {
      window.alert('无法连接后端，请确认服务已启动')
      return false
    } finally {
      setSending(false)
    }
  }

  /** 正式启动 Sprint（确认开工后调用） */
  async function beginSprint(payload: string) {
    const settings = getSprintSettingsSnapshot()
    const budget = settings.budgetText.trim() ? Number(settings.budgetText) : 0
    if (settings.budgetText.trim() && (!Number.isFinite(budget) || budget < 0)) {
      showToast('预算上限请输入非负数字（元），留空表示不限；可在左下角 ⚙ 任务设置中修改')
      return
    }
    const effectiveSaveDir = settings.saveDir === '__custom__' ? settings.customDir.trim() : settings.saveDir
    if (settings.saveDir === '__custom__' && !effectiveSaveDir) {
      showToast('请先在左下角 ⚙ 任务设置中填写自定义保存路径')
      return
    }
    requestNotifyPermission()
    setSending(true)
    const error = await startSprint(
      payload,
      attachments.map((item) => item.id),
      settings.fast,
      effectiveSaveDir,
      budget,
      settings.taskKind,
      settings.executionMode,
      settings.executionStyle,
    )
    setSending(false)
    if (error) {
      showToast(error)
      return
    }
    showToast('任务已启动，产品经理正在拆解需求')
    // 历史对话与产物保留，后续要求仍可定位它们。
    setText('')
    setAttachments([])
  }

  /** 闲聊回复：产品经理（roster 1）以流式对话回应，不触发任何任务 */
  async function replyAsChat(message: string, context = '') {
    const history = chatMessages.slice(-12).map((m) => ({
      role: m.role === 'user' ? 'user' : 'assistant',
      content: m.content,
    }))
    if (context) history.push({ role: 'assistant', content: context })
    appendConversationResult(0, '')
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ roster: 1, message, history }),
      })
      if (!res.ok || !res.body) throw new Error('连接失败，请确认后端已启动')
      let acc = ''
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      // 逐块读取 SSE，把 delta 追加到最后一条消息
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        // SSE 以双换行分隔事件
        const events = buffer.split('\n\n')
        buffer = events.pop() ?? ''
        for (const evt of events) {
          const line = evt.split('\n').find((l) => l.startsWith('data: '))
          if (!line) continue
          try {
            const payload = JSON.parse(line.slice(6)) as { delta?: string }
            if (payload.delta) {
              acc += payload.delta
              updateLastAssistantMessage(0, acc, 'llm')
            }
          } catch {
            // 单条事件解析失败不影响后续
          }
        }
      }
      if (!acc) throw new Error('未收到模型回复')
    } catch (error) {
      updateLastAssistantMessage(0, `本轮未完成：${(error as Error).message}`, 'mock')
    }
  }

  /** 发送：运行中下达指示；空闲时自动识别意图——闲聊产品经理回复，需求直接开工 */
  async function handleSend() {
    const raw = text.trim()
    if (!raw || sending || pendingRosters.includes(0)) return
    const payload = manualTarget ? `请${displayNameOf(manualTarget)}重点负责：${raw}` : raw
    if (running) {
      const ok = await sendInstruct(payload)
      if (ok) {
        appendUserMessage(0, raw)
        setText('')
      }
      return
    }
    const controller = beginChat(0)
    if (!controller) return
    setSending(true)
    const previousMessages = getThreadSnapshot(0)
    appendUserMessage(0, raw)
    setText('')
    try {
      const result = await runConversationTurn({ thread: 0, message: raw, history: previousMessages,
        uploadIds: attachments.map(item => item.id), sprintId: sprintKey || '', signal: controller.signal })
      if (result.handled) {
        appendConversationResult(0, result.reply || '本轮操作已完成', result.artifacts)
        if (result.artifacts?.length) setArtifactTarget(0, '')
        setAttachments([])
      } else if (result.intent === 'requirement') {
        await beginSprint(manualTarget ? `请${displayNameOf(manualTarget)}重点负责：${result.requirement || raw}` : result.requirement || payload)
      } else {
        await replyAsChat(raw, result.context)
      }
    } catch (error) {
      appendConversationResult(0, `本轮未完成：${(error as Error).message}。请重新发送。`)
    } finally {
      setSending(false)
      finishChat(0)
    }
  }

  return (
    <div className="view-page talk-page">
      <div className="talk-app">
        <header className="talk-head">
          <div className="talk-brand" style={{ background: hexColor(AGENT_ROSTER[0]?.color ?? 0x1d2129) }}>
            {avatarOf(1) || displayNameOf(1).slice(0, 1)}
          </div>
          <div className="talk-titles">
            <div className="t">与产品经理的对话</div>
          </div>
          <div className="spacer" />
          <button type="button" className="talk-office-btn" onClick={() => setSheetOpen(true)}>
            <span className="dot" />
            办公室员工
            <span className="cnt">6</span>
          </button>
        </header>

        <main className="talk-chat" ref={listRef}>

          {hasSprint ? (
            <>
              <div className="talk-msg user">
                <div className="talk-avatar" style={{ background: '#1d2129' }}>我</div>
                <div className="talk-msg-body">
                  <div className="talk-bubble">{requirement}</div>
                </div>
              </div>

              <div className="talk-msg ai">
                <div className="talk-avatar" style={{ background: hexColor(AGENT_ROSTER[0]?.color ?? 0x1d2129) }}>
                  {avatarOf(1) || displayNameOf(1).slice(0, 1)}
                </div>
                <div className="talk-msg-body">
                  {/* 调度记录（真实事件流，可折叠） */}
                  <div className={`talk-think ${thinkOpen ? 'open' : ''}`}>
                    <div className="talk-think-head" onClick={() => setThinkOpen((open) => !open)}>
                      {running ? <span className="spin" /> : null}
                      <span>{running ? '产品经理调度中…' : '调度记录（点击展开/收起）'}</span>
                    </div>
                    <div className="talk-think-body">
                      {activities.length === 0
                        ? '暂无调度记录'
                        : [...activities]
                            .slice(0, 6)
                            .reverse()
                            .map((a) => `${a.time} ${a.text}`)
                            .join('\n')}
                    </div>
                  </div>

                  {/* 任务交接卡片（真实任务与负责人） */}
                  {tasks.map((task) => (
                    <HandoffCard key={task.id} task={task} running={running} nameOf={displayNameOf} avatarOf={avatarOf} roleOf={roleOf} />
                  ))}

                  {/* 产物卡片（真实产物） */}
                  {(running ? artifacts : []).map((artifact) => (
                    <ArtifactCard key={artifact.id} artifact={artifact} running={running} />
                  ))}

                  {/* 汇总气泡 */}
                  <div className="talk-bubble">
                    {running ? (
                      <>
                        {`团队正在推进：当前阶段「${stage || '进行中'}」，任务通过 ${done}/${total}，累计花费 ¥${cost.toFixed(2)}。可继续补充指示，或用下方按钮暂停/结束。`}
                        <span className="talk-caret" />
                      </>
                    ) : (
                      `本轮任务已结束：${done}/${total} 项任务通过验收，共产出 ${artifacts.length} 件产物，累计花费 ¥${cost.toFixed(2)}。可在下方产物卡中预览、下载，或继续发消息修改。`
                    )}
                  </div>
                </div>
              </div>

            </>
          ) : null}
          {/* 持久化的连续对话与产物版本 */}
          {chatMessages.filter(m => !hasSprint || m.role !== 'user' || m.content !== requirement).map((m, i) => (
            <div className={`talk-msg ${m.role === 'user' ? 'user' : 'ai'}`} key={`chat-${i}`}>
              <div
                className="talk-avatar"
                style={{
                  background:
                    m.role === 'user' ? '#1d2129' : hexColor(AGENT_ROSTER[0]?.color ?? 0x1d2129),
                }}
              >
                {m.role === 'user' ? '我' : avatarOf(1) || displayNameOf(1).slice(0, 1)}
              </div>
              <div className="talk-msg-body">
                <div className="talk-bubble">{m.content}</div>
                {m.artifacts?.map(artifact => <ConversationArtifact key={artifact.id} artifact={artifact} thread={0} />)}
              </div>
            </div>
          ))}

        </main>

        <footer className="talk-foot">
          {target ? <p className="chat-hint">正在基于 {target} 修改 <button type="button" onClick={() => setArtifactTarget(0, '')}>取消指定</button></p> : null}
          {sending || pendingRosters.includes(0) ? <p role="status">正在处理本轮需求…</p> : null}
          {attachments.length > 0 || uploading ? (
            <ul className="attachment-chips talk-attach-chips">
              {attachments.map((item) => (
                <li key={item.id} className="attachment-chip">
                  <span className="attachment-name" title={item.name}>
                    {item.name}
                  </span>
                  <em>{formatSize(item.size)}</em>
                  <button
                    type="button"
                    aria-label="移除附件"
                    onClick={() => void handleRemoveAttachment(item.id)}
                  >
                    ×
                  </button>
                </li>
              ))}
              {uploading ? <li className="attachment-hint">上传中…</li> : null}
            </ul>
          ) : null}
          <div className="talk-input-row">
            <div className="talk-input-wrap">
            <div className="talk-tools" ref={toolsRef}>
              <input ref={fileInputRef} type="file" multiple hidden onChange={(event) => void handlePickAttachments(event)} />
              <input
                ref={dirInputRef}
                type="file"
                hidden
                // @ts-expect-error 非标准属性，浏览器用于文件夹上传
                webkitdirectory=""
                onChange={(event) => void handlePickAttachments(event)}
              />
              <button
                type="button"
                className="talk-plus"
                title="工具与附件"
                aria-label="工具与附件"
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((open) => !open)}
              >
                +
              </button>
              {menuOpen ? (
                <ul className="talk-menu">
                  {QUICK_TOOLS.map((tool) => (
                    <li key={tool.label}>
                      <button
                        type="button"
                        onClick={() => {
                          setMenuOpen(false)
                          handleQuickTool(tool.label)
                        }}
                      >
                        <span>{tool.icon}</span>
                        <span>{tool.label}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
            <textarea
              className="talk-input"
              rows={1}
              value={text}
              placeholder={
                running
                  ? '补充指示，例如：周报里加上本周风险…'
                  : '和团队聊聊，或描述想完成的任务（自动识别，无需确认）…'
              }
              disabled={sending || pendingRosters.includes(0)}
              onChange={(event) => {
                setText(event.target.value)
                const el = event.target
                el.style.height = 'auto'
                el.style.height = `${Math.min(el.scrollHeight, 150)}px`
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  void handleSend()
                }
              }}
            />
            <button
              type="button"
              className={`talk-voice ${listening ? 'listening' : ''}`}
              title={listening ? '正在聆听…' : '语音输入'}
              aria-label="语音输入"
              disabled={sending || pendingRosters.includes(0)}
              onClick={handleVoice}
            >
              ♩
            </button>
            <button
              type="button"
              className={`talk-send ${running && !text.trim() ? 'stop' : ''}`}
              title={running && !text.trim() ? '结束任务' : running ? '下达指示' : '发送（自动识别闲聊/需求）'}
              aria-label={running && !text.trim() ? '结束任务' : running ? '下达指示' : '发送'}
              disabled={running && !text.trim() ? false : sending || !text.trim()}
              onClick={() => (running && !text.trim() ? handleStop() : void handleSend())}
            >
              {running && !text.trim() ? '◼' : '↑'}
            </button>
            </div>
            {running ? (
              <button
                type="button"
                className="talk-pause"
                title={paused ? '全部继续' : '全部暂停'}
                aria-label={paused ? '全部继续' : '全部暂停'}
                disabled={busy}
                onClick={() => void togglePause()}
              >
                {paused ? '▶' : 'Ⅱ'}
              </button>
            ) : null}
          </div>
        </footer>
      </div>

      {/* 办公室员工浮层 */}
      <div
        className={`talk-overlay ${sheetOpen ? 'show' : ''}`}
        onClick={(event) => {
          if (event.target === event.currentTarget) setSheetOpen(false)
        }}
      >
        <div className="talk-sheet">
          <div className="talk-sheet-head">
            <div className="t">AI 敏捷办公室</div>
            <div className="s">6 名员工 · 点击员工可手动指派交接对象</div>
          </div>
          <div className="talk-sheet-list">
            {AGENT_ROSTER.map((entry, index) => {
              const roster = index + 1
              return (
                <button
                  key={entry.name}
                  type="button"
                  className={`talk-mrow online ${manualTarget === roster ? 'active' : ''}`}
                  onClick={() => {
                    setManualTarget(roster)
                    setSheetOpen(false)
                  }}
                >
                  <div className="mava" style={{ background: hexColor(entry.color) }}>
                    {avatarOf(roster) || displayNameOf(roster).slice(0, 1)}
                  </div>
                  <div className="minfo">
                    <div className="mname">
                      {displayNameOf(roster)}
                      <span className="badge sub">可交接</span>
                    </div>
                    <div className="mrole">{entry.role}</div>
                  </div>
                  <div className="mst" />
                </button>
              )
            })}
          </div>
          <div className="talk-sheet-foot">
            <label className="auto-chip">
              <input
                type="checkbox"
                checked={manualTarget === null}
                onChange={(event) => {
                  if (event.target.checked) setManualTarget(null)
                }}
              />
              由产品经理自动匹配
            </label>
          </div>
        </div>
      </div>
    </div>
  )
}

function HandoffCard({
  task,
  running,
  nameOf,
  avatarOf,
  roleOf,
}: {
  task: SprintTask
  running: boolean
  nameOf: (roster: number) => string
  avatarOf: (roster: number) => string
  roleOf: (roster: number) => string
}) {
  const meta = displayTaskMeta(task.status, running)
  const roster = task.assignee_roster ?? null
  const name = roster ? nameOf(roster) : '待分配'
  const color = roster ? hexColor(AGENT_ROSTER[roster - 1]?.color ?? 0x888888) : '#8a919f'
  const working = running && !meta.done
  return (
    <div className="talk-handoff">
      <span className="flow">交接</span>
      <div className="hava" style={{ background: color }}>
        {(roster && avatarOf(roster)) || name.slice(0, 1)}
      </div>
      <div className="hinfo">
        <div className="hname">
          {name}
          {roster ? <span className="tag">{roleOf(roster)}</span> : null}
        </div>
        <div className="hrole">{task.title}</div>
      </div>
      <div className="hst">
        {working ? (
          <>
            <span className="loading" />
            {meta.label}
          </>
        ) : (
          <span className={meta.tone === 'green' ? 'ok' : ''}>{meta.done ? `✓ ${meta.label}` : meta.label}</span>
        )}
      </div>
    </div>
  )
}

function ArtifactCard({ artifact, running }: { artifact: OfficeArtifact; running: boolean }) {
  const passed = artifact.quality_status === 'passed'
  const failed = artifact.quality_status === 'failed'
  const note = artifact.degraded
    ? `⚠ 兜底生成${artifact.degraded_reason ? `：${artifact.degraded_reason}` : ''}，可在交付页重试`
    : failed
      ? '⚠ 质检未通过，可在交付页重试'
      : passed
        ? '✓ 已生成 · 交付页可下载'
        : running
          ? '生成中…'
          : '已生成 · 交付页可下载'
  return (
    <div className="talk-artifact">
      <div className="at">
        <span className="file">{artifact.file_name || artifact.title}</span>
      </div>
      <div className="bar">
        <i style={{ width: passed || !running ? '100%' : '60%' }} />
      </div>
      <div className="ap">{note}</div>
      <ConversationArtifact artifact={artifact} thread={0} />
    </div>
  )
}
