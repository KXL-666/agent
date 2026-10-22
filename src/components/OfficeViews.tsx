import { useEffect, useState, useSyncExternalStore, type FormEvent } from 'react'
import { createPortal } from 'react-dom'
import { OfficeChatView } from '@/components/OfficeChatView'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { captureOfficeScreenshot } from '@/scene/officeSceneBridge'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'
import { setSelectedRoster } from '@/store/officeChatStore'
import {
  getArtifactsSnapshot,
  isRealArtifact,
  removeArtifact,
  setArtifacts,
  subscribeArtifacts,
  upsertArtifact,
  type OfficeArtifact,
} from '@/store/officeArtifactStore'
import type { OfficeUpload } from '@/store/officeUploadStore'
import { AttachmentPicker } from './AttachmentPicker'
import { AgentConfigDialog } from './AgentConfigDialog'
import {
  ensureAgentConfigsLoaded,
  getAgentConfigsSnapshot,
  subscribeAgentConfigs,
} from '@/store/officeAgentConfigStore'
import {
  clearNotifications,
  getNotificationsSnapshot,
  markNotificationsRead,
  subscribeNotifications,
} from '@/store/officeEventStore'
import { getOfficeAgents } from '@/store/officeStore'
import {
  assigneeName,
  getActivitiesSnapshot,
  getSprintMetaSnapshot,
  getTasksSnapshot,
  isSprintRunning,
  statusMeta,
  subscribeSprintData,
  type SprintTask,
} from '@/store/officeTaskStore'
import { showToast } from '@/store/officeUiStore'
import {
  WORKSPACES,
  getActiveWorkspace,
  getViewedSprint,
  openSprintDialog,
  setActiveView,
  setViewedSprint,
  subscribeView,
  workspaceMatches,
  type OfficeView,
} from '@/store/officeViewStore'
import { TaskDetailDialog } from './OfficeDashboardChrome'

const AGENT_STATE_LABELS: Record<string, string> = {
  idle: '空闲',
  walking: '走动中',
  working: '工作中',
  talking: '沟通中',
  thinking: '思考中',
}

function hexColor(color: number): string {
  return `#${color.toString(16).padStart(6, '0')}`
}

function timeLabel(ts: number): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

/* ------------------------------- 任务中心 ------------------------------- */

const STATUS_ORDER = ['待分派', '待开发', '待测试', '打回', '通过']

function WideTaskCard({ task, onOpen }: { task: SprintTask; onOpen?: () => void }) {
  const meta = statusMeta(task.status)
  const cancelled = task.status === '已取消'
  return (
    <article
      className="agent-card task-card-clickable"
      role={onOpen ? 'button' : undefined}
      onClick={onOpen}
    >
      <header>
        <div>
          <strong className={cancelled ? 'task-title-cancelled' : undefined}>
            {task.title || task.id}
          </strong>
          <em>
            {assigneeName(task.assignee_roster)} · {task.id}
          </em>
        </div>
        <span
          className={`agent-state-chip ${
            cancelled ? 'state-cancelled' : meta.done ? 'state-idle' : 'state-working'
          }`}
        >
          {meta.label}
        </span>
      </header>
      <p className="agent-task">{task.summary || '暂无描述'}</p>
      <div className="task-progress">
        <span
          className={`tone-${meta.tone}`}
          style={{ width: `${meta.progress}%` }}
        />
      </div>
      <em className="task-percent">{meta.progress}%</em>
    </article>
  )
}

function TaskCenterView() {
  const tasks = useSyncExternalStore(subscribeSprintData, getTasksSnapshot)
  const workspaceIndex = useSyncExternalStore(subscribeView, getActiveWorkspace)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const [detailTask, setDetailTask] = useState<SprintTask | null>(null)
  const workspace = WORKSPACES[workspaceIndex]
  const filtered = tasks.filter((task) =>
    workspaceMatches(task.assignee_roster),
  )

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>任务中心</h1>
          <p>
            LangGraph 编排的实时任务看板
            {workspaceIndex > 0 ? ` · ${workspace.label}` : ''}
          </p>
        </div>
        <button
          type="button"
          className="view-primary-btn new-sprint-btn"
          onClick={() => openSprintDialog()}
        >
          + 新建 Sprint
        </button>
      </header>

      {viewedSprint ? (
        <div className="view-history-banner">
          正在查看历史 Sprint（只读），实时进展请
          <button type="button" onClick={() => void setViewedSprint('')}>
            返回当前 Sprint
          </button>
        </div>
      ) : null}

      <div className="view-chips">
        {STATUS_ORDER.map((status) => (
          <span key={status} className="view-chip">
            {status}
            <strong>{tasks.filter((t) => t.status === status).length}</strong>
          </span>
        ))}
      </div>

      {filtered.length === 0 ? (
        <div className="view-empty">
          {tasks.length === 0
            ? '还没有任务。点击「新建 Sprint」描述需求，产品经理会自动拆解。'
            : '当前工作空间没有相关任务，可切回「我的空间」查看全部。'}
        </div>
      ) : (
        <div className="view-task-grid">
          {filtered.map((task) => (
            <WideTaskCard
              key={task.id}
              task={task}
              onOpen={() => setDetailTask(task)}
            />
          ))}
        </div>
      )}
      {detailTask ? (
        <TaskDetailDialog task={detailTask} onClose={() => setDetailTask(null)} />
      ) : null}
    </div>
  )
}

/* -------------------------------- 智能体 -------------------------------- */

function AgentsView() {
  const [, forceTick] = useState(0)
  const [configFor, setConfigFor] = useState<number | null>(null)
  const agentConfigs = useSyncExternalStore(
    subscribeAgentConfigs,
    getAgentConfigsSnapshot,
  )
  useEffect(() => {
    ensureAgentConfigsLoaded()
    const timer = setInterval(() => forceTick((n) => n + 1), 800)
    return () => clearInterval(timer)
  }, [])

  function openChat(index: number) {
    setSelectedRoster(index + 1)
    setActiveView('chat')
  }

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>智能体</h1>
          <p>6 名敏捷团队成员的实时状态（每 0.8 秒同步场景数据）</p>
        </div>
      </header>
      <div className="agent-grid">
        {AGENT_ROSTER.map((entry, index) => {
          const agent = getOfficeAgents()[index]
          const rosterNo = index + 1
          const cfg = agentConfigs[rosterNo]
          const displayName = cfg?.name || entry.name
          const avatarEmoji = cfg?.avatar || ''
          return (
            <article key={entry.id} className="agent-card">
              <header>
                <span
                  className="agent-avatar"
                  style={{ background: hexColor(entry.color) }}
                >
                  {avatarEmoji ? (
                    <span className="agent-avatar-emoji">{avatarEmoji}</span>
                  ) : (
                    displayName.slice(0, 1)
                  )}
                </span>
                <div>
                  <strong>{displayName}</strong>
                  <em>{entry.role}</em>
                </div>
                <span className={`agent-state-chip state-${agent?.state ?? 'idle'}`}>
                  {AGENT_STATE_LABELS[agent?.state ?? 'idle'] ?? '空闲'}
                </span>
              </header>
              <p className="agent-task">{agent?.currentTask || '暂无任务'}</p>
              <div className="agent-skill-row">
                {entry.skills.map((skill) => (
                  <span key={skill} className="view-chip small">{skill}</span>
                ))}
                {cfg?.has_custom_key ? (
                  <span className="view-chip small agent-custom-api">专属 API</span>
                ) : null}
                {cfg?.backup_in_use ? (
                  <span
                    className="view-chip small agent-backup-active"
                    title="主 API 连续失败，已自动切换备用通道"
                  >
                    ⚠ 备用生效中
                  </span>
                ) : cfg?.has_backup_key ? (
                  <span className="view-chip small agent-backup-api">有备用</span>
                ) : null}
              </div>
              <footer>
                <button type="button" onClick={() => openChat(index)}>
                  找 TA 聊聊
                </button>
                <button
                  type="button"
                  className="agent-settings-btn"
                  onClick={() => setConfigFor(rosterNo)}
                >
                  ⚙ 设置
                </button>
              </footer>
            </article>
          )
        })}
      </div>
      {configFor !== null ? (
        <AgentConfigDialog
          config={
            agentConfigs[configFor] ?? {
              roster: configFor,
              name: AGENT_ROSTER[configFor - 1]?.name ?? '',
              role: AGENT_ROSTER[configFor - 1]?.role ?? '',
              llm: { base_url: '', api_key: '', model: '' },
              has_custom_key: false,
            }
          }
          onClose={() => setConfigFor(null)}
        />
      ) : null}
    </div>
  )
}

/* ------------------------------- 文件中心 ------------------------------- */

type FilesPayload = {
  requirement: string
  running: boolean
  updated_at: number
  tasks: SprintTask[]
  artifacts: OfficeArtifact[]
  report_md: string
}

const ARTIFACT_KIND_LABELS: Record<OfficeArtifact['kind'], string> = {
  prd: 'PRD',
  dev: '开发',
  test: '测试',
  review: '评审',
  summary: '总结',
  custom: '生成',
  final: '交付物',
}

/** 有真实文件时走后端下载，否则退回 Markdown Blob */
function downloadArtifact(artifact: OfficeArtifact) {
  if (artifact.file_name) {
    const anchor = document.createElement('a')
    anchor.href = `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/download`
    anchor.download = artifact.file_name
    anchor.click()
    showToast('真实文件已开始下载')
    return
  }
  const blob = new Blob([artifact.content], {
    type: 'text/markdown;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${artifact.title}.md`
  anchor.click()
  URL.revokeObjectURL(url)
  showToast('产物已下载')
}

/** 从 file_name 取扩展名用于徽标展示 */
function fileExt(artifact: OfficeArtifact): string | null {
  if (!artifact.file_name) return null
  const dot = artifact.file_name.lastIndexOf('.')
  return dot >= 0 ? artifact.file_name.slice(dot) : null
}

function ArtifactCard({ artifact }: { artifact: OfficeArtifact }) {
  const ext = fileExt(artifact)
  return (
    <details className="artifact-card">
      <summary>
        <span className={`artifact-kind kind-${artifact.kind}`}>
          {ARTIFACT_KIND_LABELS[artifact.kind] ?? '产物'}
        </span>
        <strong>{artifact.title}</strong>
        {ext ? <span className="artifact-file-badge">{ext}</span> : null}
        <em>
          {assigneeName(artifact.author_roster)}
          {artifact.task_id ? ` · ${artifact.task_id}` : ''}
        </em>
      </summary>
      <pre className="view-md artifact-content">{artifact.content}</pre>
      {artifact.file_name ? (
        <footer className="artifact-card-actions">
          <button type="button" onClick={() => downloadArtifact(artifact)}>
            下载 {ext}
          </button>
        </footer>
      ) : null}
    </details>
  )
}

function FilesView() {
  const [data, setData] = useState<FilesPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const artifacts = useSyncExternalStore(
    subscribeArtifacts,
    getArtifactsSnapshot,
  )

  async function load() {
    setLoading(true)
    try {
      if (!viewedSprint) {
        const res = await fetch(`${OFFICE_API_BASE}/api/files`)
        if (!res.ok) throw new Error(String(res.status))
        const payload = (await res.json()) as FilesPayload
        setData(payload)
        setArtifacts(payload.artifacts ?? [])
      } else {
        // 历史 Sprint：无日报，需求/任务/产物取自详情接口
        const res = await fetch(`${OFFICE_API_BASE}/api/sprints/${viewedSprint}`)
        if (!res.ok) throw new Error(String(res.status))
        const payload = (await res.json()) as {
          sprint?: { requirement?: string; started_at?: number }
          tasks?: SprintTask[]
          artifacts?: OfficeArtifact[]
        }
        setData({
          requirement: payload.sprint?.requirement ?? '',
          running: false,
          updated_at: payload.sprint?.started_at ?? 0,
          tasks: payload.tasks ?? [],
          artifacts: payload.artifacts ?? [],
          report_md: '',
        })
        setArtifacts(payload.artifacts ?? [])
      }
      setError(null)
    } catch {
      setError('无法连接后端，请确认服务已启动')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [viewedSprint])

  function downloadReport() {
    if (!data?.report_md) return
    const blob = new Blob([data.report_md], {
      type: 'text/markdown;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'sprint-report.md'
    anchor.click()
    URL.revokeObjectURL(url)
    showToast('Sprint 日报已下载')
  }

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>文件中心</h1>
          <p>Sprint 产物（PRD / 开发说明 / 测试报告 / 评审记录 / 总结）、任务清单与日报</p>
        </div>
        <div className="view-head-actions">
          <button type="button" className="view-ghost-btn" onClick={load}>
            {loading ? '刷新中…' : '刷新'}
          </button>
          <button
            type="button"
            className="view-primary-btn"
            onClick={downloadReport}
            disabled={!data?.report_md}
          >
            下载日报
          </button>
        </div>
      </header>

      {error ? <div className="view-empty">{error}</div> : null}

      {!error && data ? (
        data.requirement || data.tasks.length > 0 || artifacts.length > 0 ? (
          <>
            <section className="view-card">
              <h2>需求描述</h2>
              <p className="view-card-text">
                {data.requirement || '（未记录）'}
              </p>
              <p className="view-card-meta">
                状态：{data.running ? 'Sprint 进行中' : '空闲/已完成'} ·
                更新于 {timeLabel(data.updated_at)}
              </p>
            </section>

            <section className="view-card">
              <h2>Sprint 产物（{artifacts.length}）</h2>
              {artifacts.length === 0 ? (
                <p className="view-card-meta">
                  暂无产物。Sprint 运行中，各环节（拆解 / 开发 / 测试 / 评审 / 复盘）完成后会实时出现在这里。
                </p>
              ) : (
                <>
                  <div className="artifact-list">
                    {artifacts.map((artifact) => (
                      <ArtifactCard key={artifact.id} artifact={artifact} />
                    ))}
                  </div>
                  <p className="view-card-meta">
                    <a
                      href={`${OFFICE_API_BASE}/api/artifacts/code.zip`}
                      download="sprint-code.zip"
                    >
                      下载本次 Sprint 全部代码（zip）
                    </a>
                  </p>
                </>
              )}
            </section>

            <section className="view-card">
              <h2>任务清单（{data.tasks.length}）</h2>
              {data.tasks.length === 0 ? (
                <p className="view-card-meta">暂无任务</p>
              ) : (
                <ul className="view-task-rows">
                  {data.tasks.map((task) => {
                    const meta = statusMeta(task.status)
                    return (
                      <li key={task.id}>
                        <span className={`tone-dot ${meta.tone}`} />
                        <strong>{task.title || task.id}</strong>
                        <em>{assigneeName(task.assignee_roster)}</em>
                        <span>{task.status}</span>
                      </li>
                    )
                  })}
                </ul>
              )}
            </section>

            <section className="view-card">
              <h2>Sprint 日报预览</h2>
              <pre className="view-md">{data.report_md}</pre>
            </section>
          </>
        ) : (
          <div className="view-empty">
            还没有 Sprint 产物。发起一次 Sprint 后，需求、任务、各环节产物与日报会出现在这里。
          </div>
        )
      ) : null}
    </div>
  )
}

/* ------------------------------- 技能中心 ------------------------------- */

type HealthPayload = { ok: boolean; running: boolean; llm: boolean; ws_clients: number }

type AgentApiInfo = {
  roster: number
  name: string
  role: string
  has_custom_key: boolean
  llm?: { model?: string; base_url?: string }
}

function SkillsView() {
  const [health, setHealth] = useState<HealthPayload | null>(null)
  const [agents, setAgents] = useState<AgentApiInfo[]>([])
  // 订阅成员配置：自定义头像/姓名保存后无需刷新即可反映到名册卡片
  const agentConfigs = useSyncExternalStore(
    subscribeAgentConfigs,
    getAgentConfigsSnapshot,
  )

  useEffect(() => {
    ensureAgentConfigsLoaded()
    let cancelled = false
    fetch(`${OFFICE_API_BASE}/api/health`)
      .then((res) => res.json())
      .then((data: HealthPayload) => {
        if (!cancelled) setHealth(data)
      })
      .catch(() => undefined)
    fetch(`${OFFICE_API_BASE}/api/agents`)
      .then((res) => res.json())
      .then((data: { agents?: AgentApiInfo[] }) => {
        if (!cancelled) setAgents(data.agents ?? [])
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [])

  const configured = agents.filter((a) => a.has_custom_key)
  const unconfigured = agents.filter((a) => !a.has_custom_key)
  const llmLabel =
    agents.length === 0
      ? health == null
        ? '检测中…'
        : health.llm
          ? '已接入（真实生成）'
          : 'Mock 模式（未配置任何 LLM API）'
      : configured.length === 0
        ? 'Mock 模式（全员未配置专属 API）'
        : unconfigured.length === 0
          ? `全员已配置专属 API（${configured.length}/${agents.length}）`
          : `已配置专属 API：${configured.length}/${agents.length} 名成员，其余 Mock 兜底`

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>技能中心</h1>
          <p>团队角色分工与系统能力总览</p>
        </div>
      </header>

      <section className="view-card">
        <h2>系统引擎</h2>
        <ul className="skill-engine-list">
          <li>
            <span>LLM 决策</span>
            <strong className={configured.length > 0 || health?.llm ? 'ok' : 'warn'}>
              {llmLabel}
            </strong>
          </li>
          {configured.length > 0 ? (
            <li>
              <span>已配置成员</span>
              <strong className="ok">
                {configured
                  .map((a) => `${a.name}${a.llm?.model ? `（${a.llm.model}）` : ''}`)
                  .join('、')}
              </strong>
            </li>
          ) : null}
          {unconfigured.length > 0 && agents.length > 0 ? (
            <li>
              <span>未配置成员</span>
              <strong className="warn">
                {unconfigured.map((a) => a.name).join('、')}（可在「智能体」页配置）
              </strong>
            </li>
          ) : null}
          <li>
            <span>流程编排</span>
            <strong className="ok">LangGraph 状态机（拆解→开发→测试→评审）</strong>
          </li>
          <li>
            <span>实时通信</span>
            <strong className="ok">
              WebSocket 推送（{health ? `${health.ws_clients} 个客户端在线` : '—'}）
            </strong>
          </li>
          <li>
            <span>Sprint 状态</span>
            <strong className={health?.running ? 'run' : 'ok'}>
              {health == null ? '—' : health.running ? '进行中' : '空闲'}
            </strong>
          </li>
        </ul>
      </section>

      <div className="agent-grid">
        {AGENT_ROSTER.map((entry, index) => {
          const cfg = agentConfigs[index + 1]
          const displayName = cfg?.name || entry.name
          const avatarEmoji = cfg?.avatar || ''
          return (
          <article key={entry.id} className="agent-card">
            <header>
              <span
                className="agent-avatar"
                style={{ background: hexColor(entry.color) }}
              >
                {avatarEmoji ? (
                  <span className="agent-avatar-emoji">{avatarEmoji}</span>
                ) : (
                  displayName.slice(0, 1)
                )}
              </span>
              <div>
                <strong>{displayName}</strong>
                <em>{entry.role}</em>
              </div>
            </header>
            <div className="agent-skill-row">
              {entry.skills.map((skill) => (
                <span key={skill} className="view-chip small">{skill}</span>
              ))}
            </div>
          </article>
          )
        })}
      </div>
    </div>
  )
}

/* ----------------------------- 浏览器助手 ----------------------------- */

function BrowserView() {
  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>浏览器助手</h1>
          <p>打开与本项目相关的在线资源（新标签页）</p>
        </div>
      </header>
      <div className="browser-grid">
        <a
          className="browser-card"
          href={`${OFFICE_API_BASE}/docs`}
          target="_blank"
          rel="noreferrer"
        >
          <strong>后端 API 文档</strong>
          <p>FastAPI 自动生成的交互式接口文档（Swagger UI）</p>
          <em>{OFFICE_API_BASE}/docs</em>
        </a>
        <a
          className="browser-card"
          href="https://github.com/KXL-666/ai-office-react"
          target="_blank"
          rel="noreferrer"
        >
          <strong>前端场景源码仓库</strong>
          <p>ai-office-react：PixiJS + Spine 的办公室可视化舞台</p>
          <em>github.com/KXL-666/ai-office-react</em>
        </a>
        <button
          type="button"
          className="browser-card"
          onClick={() => {
            const dataUrl = captureOfficeScreenshot()
            if (!dataUrl) {
              showToast('画布尚未就绪，请先回到首页')
              return
            }
            const anchor = document.createElement('a')
            anchor.href = dataUrl
            anchor.download = `office-snapshot-${Date.now()}.png`
            anchor.click()
            showToast('办公室画布截图已导出')
          }}
        >
          <strong>导出画布截图</strong>
          <p>把当前办公室画面保存为 PNG 图片</p>
          <em>无需联网，本地即时生成</em>
        </button>
      </div>
    </div>
  )
}

/* ------------------------------- 日程日历 ------------------------------- */

function CalendarView() {
  const notifications = useSyncExternalStore(
    subscribeNotifications,
    getNotificationsSnapshot,
  )
  const tasks = useSyncExternalStore(subscribeSprintData, getTasksSnapshot)
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)

  const milestones = [
    { label: '需求拆解', done: tasks.length > 0 },
    {
      label: '开发完成',
      done: tasks.some((t) => ['待测试', '打回', '通过'].includes(t.status)),
    },
    { label: '测试通过', done: tasks.some((t) => t.status === '通过') },
  ]

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>日程日历</h1>
          <p>Sprint 里程碑与团队动态时间线</p>
        </div>
      </header>

      <section className="view-card">
        <h2>
          本轮里程碑
          {running ? '（Sprint 进行中）' : ''}
        </h2>
        <div className="milestone-row">
          {milestones.map((item) => (
            <span
              key={item.label}
              className={`milestone ${item.done ? 'done' : ''}`}
            >
              {item.done ? '✓' : '○'} {item.label}
            </span>
          ))}
        </div>
      </section>

      <section className="view-card">
        <h2>动态时间线</h2>
        {notifications.length === 0 ? (
          <p className="view-card-meta">
            暂无记录。发起 Sprint 后，团队的每次交接与任务流转都会按时间记录在这里。
          </p>
        ) : (
          <ul className="timeline-list">
            {notifications.map((item) => (
              <li key={item.id}>
                <span className="timeline-dot" />
                <div>
                  <p>{item.text}</p>
                  <time>{item.time}</time>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

/* ------------------------------- 通知中心 ------------------------------- */

const KIND_LABELS: Record<string, string> = {
  sprint_start: 'Sprint',
  sprint_done: 'Sprint',
  sprint_error: '异常',
  desk_visit: '沟通',
  desk_visit_tour: '会议',
  task_update: '任务',
  artifact_new: '产物',
  instruction_added: '指示',
}

function NotificationsView() {
  const notifications = useSyncExternalStore(
    subscribeNotifications,
    getNotificationsSnapshot,
  )

  useEffect(() => {
    markNotificationsRead()
  }, [])

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>通知中心</h1>
          <p>后端实时推送的团队事件（WebSocket）</p>
        </div>
        <button
          type="button"
          className="view-ghost-btn"
          onClick={() => {
            clearNotifications()
            showToast('通知已清空')
          }}
        >
          清空通知
        </button>
      </header>

      {notifications.length === 0 ? (
        <div className="view-empty">暂无通知，等待团队动态。</div>
      ) : (
        <ul className="notification-list">
          {notifications.map((item) => (
            <li key={item.id}>
              <span className={`notification-kind kind-${item.kind}`}>
                {KIND_LABELS[item.kind] ?? '事件'}
              </span>
              <p>{item.text}</p>
              <time>{item.time}</time>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/* ------------------------------ 产物中心 ------------------------------ */

type UsageMember = {
  roster: number
  name?: string
  model: string
  calls: number
  prompt_tokens: number
  completion_tokens: number
  cost: number
}

type UsageReport = {
  members: UsageMember[]
  total_cost: number
  estimated: boolean
}

type PricingPayload = {
  prices: Record<string, [number, number]>
  default: [number, number]
  builtin: string[]
}

/** 单价编辑行：reset=true 表示保存时提交 null 恢复内置价 */
type PriceRow = { model: string; input: string; output: string; reset: boolean }

function UsagePanel() {
  const [usage, setUsage] = useState<UsageReport | null>(null)
  const [priceRows, setPriceRows] = useState<PriceRow[]>([])
  const [pricingLoaded, setPricingLoaded] = useState(false)
  const [pricingSaving, setPricingSaving] = useState(false)

  async function loadUsage() {
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/usage`)
      if (!res.ok) return
      setUsage((await res.json()) as UsageReport)
    } catch {
      /* 用量面板静默失败，不影响主流程 */
    }
  }

  useEffect(() => {
    loadUsage()
  }, [])

  async function loadPricing() {
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/pricing`)
      if (!res.ok) return
      const payload = (await res.json()) as PricingPayload
      setPriceRows(
        Object.entries(payload.prices ?? {}).map(([model, pair]) => ({
          model,
          input: String(pair[0]),
          output: String(pair[1]),
          reset: false,
        })),
      )
      setPricingLoaded(true)
    } catch {
      showToast('加载单价配置失败')
    }
  }

  function updateRow(index: number, patch: Partial<PriceRow>) {
    setPriceRows((rows) =>
      rows.map((row, i) => (i === index ? { ...row, ...patch } : row)),
    )
  }

  async function savePricing() {
    if (pricingSaving) return
    const prices: Record<string, [number, number] | null> = {}
    for (const row of priceRows) {
      const model = row.model.trim()
      if (!model) continue
      if (row.reset) {
        prices[model] = null
        continue
      }
      const input = Number(row.input)
      const output = Number(row.output)
      if (!Number.isFinite(input) || !Number.isFinite(output)) continue
      prices[model] = [input, output]
    }
    setPricingSaving(true)
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/pricing`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prices }),
      })
      if (!res.ok) {
        const detail = (await res.json().catch(() => null)) as {
          detail?: string
        } | null
        showToast(detail?.detail ?? '保存单价失败')
        return
      }
      showToast('单价已保存，金额已按新价重算')
      await loadPricing()
      await loadUsage()
    } catch {
      showToast('保存单价失败，请确认后端已启动')
    } finally {
      setPricingSaving(false)
    }
  }

  const totals = usage?.members.reduce(
    (acc, m) => ({
      calls: acc.calls + m.calls,
      prompt: acc.prompt + m.prompt_tokens,
      completion: acc.completion + m.completion_tokens,
    }),
    { calls: 0, prompt: 0, completion: 0 },
  )

  return (
    <details
      className="usage-panel"
      onToggle={(event) => {
        if (event.currentTarget.open) loadUsage()
      }}
    >
      <summary>
        <span>用量统计（估算）</span>
        <span className="usage-total">
          {usage
            ? `累计花费 ¥${usage.total_cost.toFixed(4)} · 点击展开明细`
            : '点击展开'}
        </span>
      </summary>
      {usage && usage.members.length > 0 && totals ? (
        <>
          <table>
            <thead>
              <tr>
                <th>成员</th>
                <th>模型</th>
                <th>调用次数</th>
                <th>输入 tokens</th>
                <th>输出 tokens</th>
                <th>估算花费</th>
              </tr>
            </thead>
            <tbody>
              {usage.members.map((m) => (
                <tr key={m.roster}>
                  <td>{m.name ?? `#${m.roster}`}</td>
                  <td>{m.model}</td>
                  <td>{m.calls}</td>
                  <td>{m.prompt_tokens}</td>
                  <td>{m.completion_tokens}</td>
                  <td>¥{m.cost.toFixed(4)}</td>
                </tr>
              ))}
              <tr className="usage-total-row">
                <td>合计</td>
                <td>—</td>
                <td>{totals.calls}</td>
                <td>{totals.prompt}</td>
                <td>{totals.completion}</td>
                <td>¥{usage.total_cost.toFixed(4)}</td>
              </tr>
            </tbody>
          </table>
          <div className="usage-note">
            费用按各服务商公开刊例约值估算，仅供演示参考，以实际账单为准。
            <a
              className="usage-export-link"
              href={`${OFFICE_API_BASE}/api/usage/export.csv`}
              download
            >
              ⇩ 导出 CSV
            </a>
          </div>
          <details
            className="pricing-panel"
            onToggle={(event) => {
              if (event.currentTarget.open && !pricingLoaded) void loadPricing()
            }}
          >
            <summary>自定义单价（元/百万tokens）</summary>
            {pricingLoaded ? (
              <>
                <table>
                  <thead>
                    <tr>
                      <th>模型</th>
                      <th>输入价</th>
                      <th>输出价</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {priceRows.map((row, index) => (
                      <tr key={index} className={row.reset ? 'pricing-row-reset' : ''}>
                        <td>
                          <input
                            value={row.model}
                            placeholder="模型名，如 gpt-4o"
                            disabled={row.reset}
                            onChange={(event) =>
                              updateRow(index, { model: event.target.value })
                            }
                          />
                        </td>
                        <td>
                          <input
                            type="number"
                            min="0"
                            step="0.01"
                            value={row.input}
                            disabled={row.reset}
                            onChange={(event) =>
                              updateRow(index, { input: event.target.value })
                            }
                          />
                        </td>
                        <td>
                          <input
                            type="number"
                            min="0"
                            step="0.01"
                            value={row.output}
                            disabled={row.reset}
                            onChange={(event) =>
                              updateRow(index, { output: event.target.value })
                            }
                          />
                        </td>
                        <td>
                          {row.reset ? (
                            <button
                              type="button"
                              onClick={() => updateRow(index, { reset: false })}
                            >
                              撤销
                            </button>
                          ) : (
                            <button
                              type="button"
                              onClick={() => updateRow(index, { reset: true })}
                            >
                              恢复内置价
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="pricing-actions">
                  <button
                    type="button"
                    onClick={() =>
                      setPriceRows((rows) => [
                        ...rows,
                        { model: '', input: '', output: '', reset: false },
                      ])
                    }
                  >
                    + 新增模型
                  </button>
                  <button
                    type="button"
                    className="pricing-save-btn"
                    disabled={pricingSaving}
                    onClick={() => void savePricing()}
                  >
                    {pricingSaving ? '保存中…' : '保存单价'}
                  </button>
                </div>
              </>
            ) : (
              <p className="usage-empty">展开后加载单价配置…</p>
            )}
          </details>
        </>
      ) : (
        <div className="usage-empty">暂无用量记录，Sprint 运行后自动统计。</div>
      )}
    </details>
  )
}

/** 简单行级 diff：基于 LCS，把两份文本对比为 same/add/del 行序列（内容已裁剪，行数有限） */
type DiffLine = { type: 'same' | 'add' | 'del'; text: string }

function diffLines(oldText: string, newText: string): DiffLine[] {
  const a = oldText.split('\n')
  const b = newText.split('\n')
  const m = a.length
  const n = b.length
  const dp: number[][] = Array.from({ length: m + 1 }, () =>
    new Array<number>(n + 1).fill(0),
  )
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] =
        a[i] === b[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      out.push({ type: 'same', text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ type: 'del', text: a[i] })
      i++
    } else {
      out.push({ type: 'add', text: b[j] })
      j++
    }
  }
  while (i < m) out.push({ type: 'del', text: a[i++] })
  while (j < n) out.push({ type: 'add', text: b[j++] })
  return out
}

/** 版本链面板：展示同一产物的全部历史版本，支持查看内容、相邻版本对比与回滚 */
function VersionChainPanel({
  artifact,
  onClose,
}: {
  artifact: OfficeArtifact
  onClose: () => void
}) {
  const [versions, setVersions] = useState<OfficeArtifact[]>([])
  const [loading, setLoading] = useState(true)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [diffFor, setDiffFor] = useState<string | null>(null)
  const [rolling, setRolling] = useState(false)

  async function load() {
    setLoading(true)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/versions`,
      )
      if (!res.ok) throw new Error(String(res.status))
      const payload = (await res.json()) as { versions?: OfficeArtifact[] }
      setVersions(payload.versions ?? [])
    } catch {
      showToast('加载版本链失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifact.id])

  async function rollback(target: OfficeArtifact) {
    if (rolling) return
    setRolling(true)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/rollback`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target_id: target.id }),
        },
      )
      const payload = (await res.json().catch(() => null)) as
        | { artifact?: OfficeArtifact; detail?: string }
        | null
      if (!res.ok) {
        showToast(payload?.detail ?? '回滚失败')
        return
      }
      if (payload?.artifact) upsertArtifact(payload.artifact)
      showToast(`已按 v${target.version ?? 1} 的内容生成回滚版本，旧版仍保留`)
      await load()
    } catch {
      showToast('回滚失败，请确认后端已启动')
    } finally {
      setRolling(false)
    }
  }

  const latestId = versions[versions.length - 1]?.id

  return (
    <div className="version-chain-panel">
      <header>
        <strong>版本链（{versions.length} 个版本）</strong>
        <button type="button" onClick={onClose}>
          收起
        </button>
      </header>
      {loading ? <p className="view-card-meta">加载中…</p> : null}
      {!loading &&
        versions.map((v, index) => {
          const prev = index > 0 ? versions[index - 1] : null
          const showDiff = diffFor === v.id && prev
          return (
            <div
              key={v.id}
              className={`version-row ${v.id === latestId ? 'latest' : ''}`}
            >
              <div className="version-row-head">
                <span className="artifact-file-badge">v{v.version ?? 1}</span>
                <strong>{v.title}</strong>
                <em>{timeLabel(v.created_at)}</em>
                {v.id === latestId ? (
                  <span className="version-latest-badge">当前最新</span>
                ) : null}
              </div>
              <div className="version-row-actions">
                <button
                  type="button"
                  onClick={() =>
                    setExpandedId(expandedId === v.id ? null : v.id)
                  }
                >
                  {expandedId === v.id ? '收起内容' : '查看内容'}
                </button>
                {prev ? (
                  <button
                    type="button"
                    onClick={() => setDiffFor(showDiff ? null : v.id)}
                  >
                    {showDiff ? '收起对比' : `对比 v${prev.version ?? index}`}
                  </button>
                ) : null}
                {v.id !== latestId ? (
                  <button
                    type="button"
                    disabled={rolling}
                    onClick={() => void rollback(v)}
                  >
                    {rolling ? '回滚中…' : '回滚到此版'}
                  </button>
                ) : null}
              </div>
              {expandedId === v.id ? (
                <pre className="view-md version-content">{v.content}</pre>
              ) : null}
              {showDiff && prev ? (
                <pre className="version-diff">
                  {diffLines(prev.content, v.content).map((line, i) => (
                    <span key={i} className={`diff-${line.type}`}>
                      {line.type === 'add' ? '+ ' : line.type === 'del' ? '- ' : '  '}
                      {line.text}
                      {'\n'}
                    </span>
                  ))}
                </pre>
              ) : null}
            </div>
          )
        })}
    </div>
  )
}

/** 产物内容编辑弹窗：载入原始全文，保存为版本链上的新「编辑 vN」版本 */
function ArtifactEditModal({
  artifact,
  onClose,
}: {
  artifact: OfficeArtifact
  onClose: () => void
}) {
  const [content, setContent] = useState('')
  const [truncated, setTruncated] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await fetch(
          `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/raw`,
        )
        if (!res.ok) throw new Error(String(res.status))
        const payload = (await res.json()) as {
          content: string
          truncated: boolean
        }
        if (cancelled) return
        setContent(payload.content)
        setTruncated(Boolean(payload.truncated))
      } catch {
        if (!cancelled) showToast('加载产物内容失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [artifact.id])

  async function save() {
    if (saving) return
    setSaving(true)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/edit`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content }),
        },
      )
      const payload = (await res.json().catch(() => null)) as {
        detail?: string
      } | null
      if (!res.ok) {
        showToast(payload?.detail ?? '保存失败')
        return
      }
      showToast('已保存为新版本', { kind: 'success' })
      onClose()
    } catch {
      showToast('保存失败，请确认后端已启动')
    } finally {
      setSaving(false)
    }
  }

  const isDocx = Boolean(artifact.file_name?.endsWith('.docx'))

  return createPortal(
    <div className="sprint-backdrop" onClick={onClose}>
      <div
        className="sprint-dialog artifact-edit-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="sprint-dialog-head">
          <h3>编辑：{artifact.title}</h3>
          <button
            type="button"
            className="sprint-dialog-close"
            aria-label="关闭"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        {truncated ? (
          <p className="artifact-edit-warning">
            ⚠ 内容超200KB被截断，保存将截断版本
          </p>
        ) : null}
        {isDocx ? (
          <p className="sprint-dialog-tip">
            保存后将自动重新生成 Word 文件（Markdown 语法：# 标题、- 列表）
          </p>
        ) : null}
        {loading ? (
          <p className="view-card-meta">内容加载中…</p>
        ) : (
          <textarea
            className="artifact-edit-textarea"
            value={content}
            onChange={(event) => setContent(event.target.value)}
          />
        )}
        <div className="sprint-dialog-actions">
          <button type="button" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary"
            disabled={loading || saving}
            onClick={() => void save()}
          >
            {saving ? '保存中…' : '保存为新版本'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

/** HTML 产物沙箱预览：iframe 仅允许脚本，隔离文档域 */
function ArtifactSandboxModal({
  artifact,
  onClose,
}: {
  artifact: OfficeArtifact
  onClose: () => void
}) {
  return createPortal(
    <div className="sprint-backdrop" onClick={onClose}>
      <div
        className="sprint-dialog artifact-sandbox-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="sprint-dialog-head">
          <h3>沙箱预览：{artifact.title}</h3>
          <button
            type="button"
            className="sprint-dialog-close"
            aria-label="关闭"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <iframe
          className="artifact-sandbox-iframe"
          sandbox="allow-scripts"
          title={`沙箱预览：${artifact.title}`}
          src={`${OFFICE_API_BASE}/api/artifacts/${artifact.id}/code_preview`}
        />
      </div>
    </div>,
    document.body,
  )
}

function ArtifactsView() {
  const [error, setError] = useState<string | null>(null)
  const [openId, setOpenId] = useState<string | null>(null)
  const [editFor, setEditFor] = useState<OfficeArtifact | null>(null)
  const [sandboxFor, setSandboxFor] = useState<OfficeArtifact | null>(null)
  const [prompt, setPrompt] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [attachments, setAttachments] = useState<OfficeUpload[]>([])
  const [busyId, setBusyId] = useState<string | null>(null)
  const [reviseFor, setReviseFor] = useState<string | null>(null)
  const [reviseText, setReviseText] = useState('')
  const [versionsFor, setVersionsFor] = useState<string | null>(null)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const sprintMeta = useSyncExternalStore(subscribeSprintData, getSprintMetaSnapshot)
  const artifacts = useSyncExternalStore(
    subscribeArtifacts,
    getArtifactsSnapshot,
  )
  const activities = useSyncExternalStore(subscribeSprintData, getActivitiesSnapshot)
  // 直接生成的最新进度（后端 generate_progress 事件已写入实时动态）
  const genProgress = activities.find((item) => item.text.startsWith('直接生成：'))

  useEffect(() => {
    // 历史/当前数据由全局切换器写入 store，这里只负责拉一次当前数据兜底
    if (viewedSprint) return
    async function load() {
      try {
        const res = await fetch(`${OFFICE_API_BASE}/api/files`)
        if (!res.ok) throw new Error(String(res.status))
        const payload = (await res.json()) as FilesPayload
        setArtifacts(payload.artifacts ?? [])
        setError(null)
      } catch {
        setError('无法连接后端，请确认服务已启动')
      }
    }
    load()
  }, [viewedSprint])

  // 只留真产物：最终交付物置顶，其余按时间倒序；过程文档请见文件中心
  const mine = artifacts.filter(isRealArtifact).sort((a, b) => {
    const fa = a.kind === 'final' ? 1 : 0
    const fb = b.kind === 'final' ? 1 : 0
    if (fa !== fb) return fb - fa
    return b.created_at - a.created_at
  })

  // 打包下载：查看历史 Sprint 用其 id，否则用当前 Sprint id
  const zipSprintId = viewedSprint || sprintMeta.id
  const hasFileArtifacts = artifacts.some((a) => Boolean(a.file_name))

  async function submitGenerate(event: FormEvent) {
    event.preventDefault()
    const text = prompt.trim()
    if (!text || submitting) return
    setSubmitting(true)
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/artifacts/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: text,
          upload_ids: attachments.map((item) => item.id),
        }),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        showToast(typeof detail?.detail === 'string' ? detail.detail : '提交失败')
        return
      }
      setPrompt('')
      setAttachments([])
      showToast('已提交，生成约需 10-60 秒，完成后自动出现在列表')
    } catch {
      showToast('提交失败，请确认后端已启动')
    } finally {
      setSubmitting(false)
    }
  }

  function download(artifact: OfficeArtifact) {
    downloadArtifact(artifact)
  }

  async function copy(artifact: OfficeArtifact) {
    try {
      await navigator.clipboard.writeText(artifact.content)
      showToast('全文已复制到剪贴板')
    } catch {
      showToast('复制失败，请手动选择文本复制')
    }
  }

  async function retry(artifact: OfficeArtifact) {
    if (busyId) return
    setBusyId(artifact.id)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/retry`,
        { method: 'POST' },
      )
      const payload = (await res.json().catch(() => null)) as
        | { artifact?: OfficeArtifact; detail?: string }
        | null
      if (!res.ok) {
        showToast(payload?.detail ?? '重新生成失败')
        return
      }
      if (payload?.artifact) upsertArtifact(payload.artifact)
      showToast('已重新生成')
    } catch {
      showToast('重新生成失败，请确认后端已启动')
    } finally {
      setBusyId(null)
    }
  }

  async function revise(artifact: OfficeArtifact) {
    const instruction = reviseText.trim()
    if (!instruction || busyId) return
    setBusyId(artifact.id)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/revise`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ instruction }),
        },
      )
      const payload = (await res.json().catch(() => null)) as
        | { artifact?: OfficeArtifact; detail?: string }
        | null
      if (!res.ok) {
        showToast(payload?.detail ?? '生成修订版失败')
        return
      }
      if (payload?.artifact) {
        upsertArtifact(payload.artifact)
        setOpenId(payload.artifact.id)
      }
      setReviseFor(null)
      setReviseText('')
      showToast('已生成修订版，原版本仍保留可追溯')
    } catch {
      showToast('生成修订版失败，请确认后端已启动')
    } finally {
      setBusyId(null)
    }
  }

  async function reveal(artifact: OfficeArtifact) {
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/reveal`,
        { method: 'POST' },
      )
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        showToast(typeof detail?.detail === 'string' ? detail.detail : '打开失败')
      }
    } catch {
      showToast('打开所在文件夹失败，请确认后端已启动')
    }
  }

  /** 结构化预览：Word/PPT/Excel 在后端解析为 HTML 新标签页展示 */
  function preview(artifact: OfficeArtifact) {
    window.open(
      `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/preview`,
      '_blank',
      'noopener',
    )
  }

  async function removeOne(artifact: OfficeArtifact) {
    const ok = window.confirm(
      `确定删除产物「${artifact.title}」？其磁盘文件将一并删除，不可恢复。`,
    )
    if (!ok) return
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}`,
        { method: 'DELETE' },
      )
      const detail = (await res.json().catch(() => null)) as {
        detail?: string
      } | null
      if (!res.ok) {
        showToast(detail?.detail ?? '删除失败')
        return
      }
      removeArtifact(artifact.id)
      showToast('产物已删除')
    } catch {
      showToast('删除失败，请确认后端已启动')
    }
  }

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>产物中心</h1>
          <p>最终交付物与真实文件（Word/PPT/Excel/代码），过程文档见文件中心</p>
        </div>
        <form className="artifact-gen-form" onSubmit={submitGenerate}>
          <input
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            placeholder="直接下指令：写份周报 / 做个 PPT / 基于附件改写…"
            disabled={submitting}
          />
          <button type="submit" disabled={submitting || !prompt.trim()}>
            {submitting ? '提交中…' : '生成'}
          </button>
        </form>
        {zipSprintId ? (
          <a
            className="view-ghost-btn artifact-zip-btn"
            href={`${OFFICE_API_BASE}/api/sprints/${zipSprintId}/artifacts.zip`}
            target="_blank"
            rel="noreferrer"
            title={hasFileArtifacts ? '打包下载该 Sprint 全部文件产物' : '该 Sprint 暂无文件产物'}
            onClick={(event) => {
              if (!hasFileArtifacts) {
                event.preventDefault()
                showToast('该 Sprint 暂无可下载的文件产物')
              }
            }}
          >
            📦 打包下载全部
          </a>
        ) : null}
      </header>

      <div className="artifact-gen-attach">
        <AttachmentPicker value={attachments} onChange={setAttachments} />
      </div>

      {genProgress ? (
        <p className="artifact-gen-progress">
          {genProgress.time} · {genProgress.text}（进度详见右侧实时动态）
        </p>
      ) : null}

      <UsagePanel />

      {error ? <div className="view-empty">{error}</div> : null}

      {!error ? (
        mine.length === 0 ? (
          <div className="view-empty">
            暂无产物。发起 Sprint，或在上方输入需求直接生成 Word / PPT / Excel
          </div>
        ) : (
          <div className="artifact-doc-list">
            {mine.map((artifact) => {
              const open = openId === artifact.id
              const degraded = Boolean(artifact.degraded)
              return (
                <article
                  key={artifact.id}
                  className={`artifact-doc ${degraded ? 'artifact-degraded' : ''}`}
                >
                  <header
                    role="presentation"
                    onClick={() => setOpenId(open ? null : artifact.id)}
                  >
                    <strong>{artifact.title}</strong>
                    {degraded ? (
                      <span className="artifact-degraded-badge">兜底生成</span>
                    ) : null}
                    {artifact.version && artifact.version > 1 ? (
                      <span className="artifact-file-badge">
                        v{artifact.version}
                      </span>
                    ) : null}
                    {fileExt(artifact) ? (
                      <span className="artifact-file-badge">
                        {fileExt(artifact)}
                      </span>
                    ) : null}
                    <em>{timeLabel(artifact.created_at)}</em>
                  </header>
                  {open ? (
                    <>
                      {degraded && artifact.degraded_reason ? (
                        <p className="artifact-degraded-tip">
                          ⚠ 本次为兜底内容：{artifact.degraded_reason}
                        </p>
                      ) : null}
                      <pre className="view-md artifact-doc-body">
                        {artifact.content}
                      </pre>
                      {reviseFor === artifact.id ? (
                        <div className="artifact-revise-row">
                          <input
                            autoFocus
                            value={reviseText}
                            placeholder="说明怎么改，例如：第二章再展开一点，补三个案例"
                            onChange={(event) => setReviseText(event.target.value)}
                            onKeyDown={(event) => {
                              if (event.key === 'Enter') void revise(artifact)
                            }}
                          />
                          <button
                            type="button"
                            disabled={busyId === artifact.id || !reviseText.trim()}
                            onClick={() => void revise(artifact)}
                          >
                            {busyId === artifact.id ? '生成中…' : '生成修订版'}
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              setReviseFor(null)
                              setReviseText('')
                            }}
                          >
                            取消
                          </button>
                        </div>
                      ) : null}
                      {versionsFor === artifact.id ? (
                        <VersionChainPanel
                          artifact={artifact}
                          onClose={() => setVersionsFor(null)}
                        />
                      ) : null}
                      <footer>
                        <button type="button" onClick={() => copy(artifact)}>
                          复制全文
                        </button>
                        <button
                          type="button"
                          className="artifact-edit-btn"
                          onClick={() => setEditFor(artifact)}
                        >
                          ✏️ 编辑
                        </button>
                        {artifact.file_name?.endsWith('.html') ? (
                          <button
                            type="button"
                            onClick={() => setSandboxFor(artifact)}
                          >
                            🧪 沙箱预览
                          </button>
                        ) : null}
                        {artifact.file_name ? (
                          <button
                            type="button"
                            onClick={() => preview(artifact)}
                          >
                            预览
                          </button>
                        ) : null}
                        <button
                          type="button"
                          onClick={() => download(artifact)}
                        >
                          {artifact.file_name
                            ? `下载 ${fileExt(artifact)}`
                            : '下载 .md'}
                        </button>
                        {artifact.file_name ? (
                          <button
                            type="button"
                            onClick={() => void reveal(artifact)}
                          >
                            打开所在文件夹
                          </button>
                        ) : null}
                        <button
                          type="button"
                          onClick={() =>
                            setVersionsFor(
                              versionsFor === artifact.id ? null : artifact.id,
                            )
                          }
                        >
                          {versionsFor === artifact.id ? '收起版本' : '版本'}
                        </button>
                        <button
                          type="button"
                          className="artifact-retry-btn"
                          disabled={busyId === artifact.id}
                          onClick={() => void retry(artifact)}
                        >
                          {busyId === artifact.id ? '重试中…' : '重新生成'}
                        </button>
                        <button
                          type="button"
                          className="artifact-revise-btn"
                          onClick={() => {
                            setReviseFor(
                              reviseFor === artifact.id ? null : artifact.id,
                            )
                            setReviseText('')
                          }}
                        >
                          继续修改
                        </button>
                        <button
                          type="button"
                          className="artifact-delete-btn"
                          onClick={() => void removeOne(artifact)}
                        >
                          删除
                        </button>
                      </footer>
                    </>
                  ) : null}
                </article>
              )
            })}
          </div>
        )
      ) : null}
      {editFor ? (
        <ArtifactEditModal artifact={editFor} onClose={() => setEditFor(null)} />
      ) : null}
      {sandboxFor ? (
        <ArtifactSandboxModal
          artifact={sandboxFor}
          onClose={() => setSandboxFor(null)}
        />
      ) : null}
    </div>
  )
}

/* ------------------------------ 视图切换器 ------------------------------ */

export function OfficeViewPanel({ view }: { view: OfficeView }) {
  switch (view) {
    case 'tasks':
      return <TaskCenterView />
    case 'agents':
      return <AgentsView />
    case 'files':
      return <FilesView />
    case 'artifacts':
      return <ArtifactsView />
    case 'skills':
      return <SkillsView />
    case 'browser':
      return <BrowserView />
    case 'chat':
      return <OfficeChatView />
    case 'calendar':
      return <CalendarView />
    case 'notifications':
      return <NotificationsView />
    default:
      return null
  }
}
