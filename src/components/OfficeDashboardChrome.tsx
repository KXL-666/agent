import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { submitVisitAction } from '@/services/officeActionDispatcher'
import { requestNotifyPermission } from '@/services/officeNotify'
import { isVoiceSupported, startVoiceRecognition } from '@/services/officeVoice'
import {
  captureOfficeScreenshot,
  setOfficePaused,
} from '@/scene/officeSceneBridge'
import {
  getUnreadSnapshot,
  subscribeNotifications,
} from '@/store/officeEventStore'
import {
  getArtifactsSnapshot,
  subscribeArtifacts,
} from '@/store/officeArtifactStore'
import type { OfficeUpload } from '@/store/officeUploadStore'
import { AttachmentPicker } from './AttachmentPicker'
import { getOfficeAgents } from '@/store/officeStore'
import {
  assigneeName,
  computeSprintProgress,
  displayTaskMeta,
  getActivitiesSnapshot,
  getSprintMetaSnapshot,
  getTasksSnapshot,
  isSprintRunning,
  subscribeSprintData,
  type SprintTask,
} from '@/store/officeTaskStore'
import {
  getToastKind,
  getToastSnapshot,
  showToast,
  subscribeToast,
} from '@/store/officeUiStore'
import {
  getTheme,
  subscribeTheme,
  toggleTheme,
} from '@/store/officeThemeStore'
import {
  WORKSPACES,
  closeSprintDialog,
  getActiveView,
  getActiveWorkspace,
  getSprintHistory,
  getViewedSprint,
  isSprintDialogOpen,
  openSprintDialog,
  refreshSprintHistory,
  setActiveView,
  setSearchOpen,
  setViewedSprint,
  subscribeView,
  takeSprintPrefill,
  workspaceMatches,
  type OfficeView,
} from '@/store/officeViewStore'

const NAV_ITEMS: { label: string; view: OfficeView }[] = [
  { label: '概览', view: 'home' },
  { label: '任务', view: 'tasks' },
  { label: '交付', view: 'artifacts' },
  { label: '团队', view: 'agents' },
  { label: '对话', view: 'chat' },
]

function SidebarIcon({ view }: { view: OfficeView }) {
  const paths: Record<string, ReactNode> = {
    home: <><path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z" /><path d="M9 21v-7h6v7" /></>,
    tasks: <><rect x="3" y="4" width="18" height="17" rx="2" /><path d="m7 9 1.5 1.5L11 8M13 10h5M7 15l1.5 1.5L11 14M13 16h5" /></>,
    artifacts: <><path d="M4 7h16v13H4zM8 7V4h8v3" /><path d="M8 12h8M8 16h5" /></>,
    agents: <><circle cx="9" cy="8" r="3" /><path d="M3 20v-2a6 6 0 0 1 12 0v2M17 6a3 3 0 0 1 0 6M17 15a5 5 0 0 1 4 5" /></>,
    chat: <path d="M4 5h16v12H9l-5 4z" />,
    notifications: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 8-3 9h18c0-1-3-2-3-9M10 21h4" /></>,
  }
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[view]}</svg>
}

const NAV_GROUPS: Record<string, OfficeView[]> = {
  home: ['home'],
  tasks: ['tasks', 'calendar'],
  artifacts: ['artifacts', 'files', 'knowledge'],
  agents: ['agents', 'skills'],
  chat: ['chat'],
}

const QUICK_TOOLS = [
  { icon: '□', label: '文件浏览' },
  { icon: '◎', label: '打开浏览器' },
  { icon: '⌗', label: '截图' },
  { icon: '♩', label: '语音输入' },
] as const

const TOOLBAR_ITEMS = [
  { icon: 'Ⅱ', label: '全部暂停', primary: false },
  { icon: '▶', label: '全部继续', primary: false },
  { icon: '◼', label: '任务结束', primary: false },
  { icon: '□', label: '安排会议', primary: false },
  { icon: '+', label: '新建任务', primary: true },
  { icon: '⇩', label: '导出日报', primary: false },
] as const

export function OfficeSidebar() {
  const tasks = useSyncExternalStore(subscribeSprintData, getTasksSnapshot)
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const unread = useSyncExternalStore(subscribeNotifications, getUnreadSnapshot)
  const activeView = useSyncExternalStore(subscribeView, getActiveView)
  const importInputRef = useRef<HTMLInputElement | null>(null)
  const [importing, setImporting] = useState(false)

  /** 导入备份 zip：成功后需重启后端并刷新页面才能看到恢复的数据 */
  async function importBackup(file: File) {
    if (importing) return
    setImporting(true)
    try {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch(`${OFFICE_API_BASE}/api/backup/import`, {
        method: 'POST',
        body: form,
      })
      const payload = (await res.json().catch(() => null)) as {
        note?: string
        detail?: string
      } | null
      if (res.status === 409) {
        window.alert(payload?.detail ?? '已有 Sprint 进行中，请先结束 Sprint 再导入备份')
        return
      }
      if (!res.ok) {
        window.alert(payload?.detail ?? '导入备份失败')
        return
      }
      window.alert(payload?.note ?? '备份已导入，请重启后端并刷新页面')
    } catch {
      window.alert('导入备份失败，请确认后端已启动')
    } finally {
      setImporting(false)
      if (importInputRef.current) importInputRef.current.value = ''
    }
  }

  const navItems = NAV_ITEMS.map((item) => {
    if (item.view === 'tasks') {
      return { ...item, badge: running && !viewedSprint && tasks.length > 0 ? tasks.length : undefined }
    }
    if (item.view === 'notifications') {
      return { ...item, badge: unread > 0 ? unread : undefined }
    }
    return { ...item, badge: undefined }
  })

  function handleNav(item: (typeof NAV_ITEMS)[number]) {
    setActiveView(item.view)
  }

  return (
    <aside className="office-sidebar">
      <div className="sidebar-brand">
        <span className="brand-mark">⬢</span>
        <span>Agile Dev Team</span>
      </div>

      <button
        type="button"
        className="sidebar-search"
        onClick={() => setSearchOpen(true)}
      >
        <span>⌕</span>
        <span>搜索功能、任务、员工…</span>
        <kbd>⌘K</kbd>
      </button>

      <nav className="sidebar-nav">
        {navItems.map((item) => (
          <button
            key={item.label}
            type="button"
            className={`nav-item ${NAV_GROUPS[item.view]?.includes(activeView) ? 'active' : ''}`}
            onClick={() => handleNav(item)}
          >
            <span className="nav-main">
              <span className="nav-icon"><SidebarIcon view={item.view} /></span>
              <span>{item.label}</span>
            </span>
            {item.badge ? (
              <span className="nav-badge">{item.badge}</span>
            ) : null}
          </button>
        ))}
      </nav>

      <div className="sidebar-footer">
        <div className="sidebar-utility-row">
          <button type="button" className={`sidebar-utility ${activeView === 'notifications' ? 'active' : ''}`} onClick={() => setActiveView('notifications')} aria-label={`通知${unread ? `，${unread} 条未读` : ''}`}>
            <SidebarIcon view="notifications" /> 通知 {unread > 0 ? <span className="nav-badge">{unread}</span> : null}
          </button>
          <details className="sidebar-more">
            <summary>⋯ 更多工具</summary>
            <button type="button" onClick={() => setActiveView('browser')}>浏览器助手</button>
            <details className="sidebar-data">
              <summary>数据管理</summary>
              <div className="sidebar-backup-row">
                <a href={`${OFFICE_API_BASE}/api/backup/export`} download>
                  💾 导出备份
                </a>
                <button
                  type="button"
                  disabled={importing}
                  onClick={() => importInputRef.current?.click()}
                >
                  {importing ? '导入中…' : '📥 导入备份'}
                </button>
              </div>
            </details>
          </details>
          <input
            ref={importInputRef}
            type="file"
            accept=".zip"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) void importBackup(file)
            }}
          />
        </div>
        <div className="user-card">
          <span className="user-avatar">T</span>
          <span>
            <strong>Teejoo</strong>
            <em><span className="agent-dot online" />在线</em>
          </span>
        </div>
        <div className="system-status">
          <span className="agent-dot online" />
          <span>本地运行正常</span>
          <span>☼</span>
        </div>
      </div>
    </aside>
  )
}

type SystemStats = { cpu: number; mem: number; net_kb_s: number }

/** 真实系统资源：3 秒轮询后端 psutil 采集 */
function OfficeSystemResources() {
  const [stats, setStats] = useState<SystemStats | null>(null)

  useEffect(() => {
    let cancelled = false
    async function poll() {
      try {
        const res = await fetch(`${OFFICE_API_BASE}/api/system`)
        if (!res.ok) throw new Error(String(res.status))
        const data = (await res.json()) as SystemStats
        if (!cancelled) setStats(data)
      } catch {
        if (!cancelled) setStats(null)
      }
    }
    poll()
    const timer = setInterval(poll, 3000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  const cpu = stats ? stats.cpu : null
  const mem = stats ? stats.mem : null
  const net = stats ? stats.net_kb_s : null

  return (
    <section className="stat-card resource-card">
      <span className="stat-label">系统资源</span>
      <div className="resource-row">
        <span>CPU</span>
        <div className="resource-bar">
          <span style={{ width: `${cpu ?? 0}%` }} />
        </div>
        <em>{cpu == null ? '--' : `${cpu}%`}</em>
      </div>
      <div className="resource-row">
        <span>内存</span>
        <div className="resource-bar purple">
          <span style={{ width: `${mem ?? 0}%` }} />
        </div>
        <em>{mem == null ? '--' : `${mem}%`}</em>
      </div>
      <div className="resource-row">
        <span>网络</span>
        <em className="network-value">
          {net == null ? '--' : `${net} KB/s`}
        </em>
      </div>
    </section>
  )
}

/** 暗色/亮色切换：写入 localStorage 并切换根元素 data-theme */
function ThemeToggle() {
  const theme = useSyncExternalStore(subscribeTheme, getTheme)
  return (
    <button
      type="button"
      className="theme-toggle"
      title={theme === 'dark' ? '切换到亮色主题' : '切换到暗色主题'}
      aria-label="切换主题"
      onClick={toggleTheme}
    >
      {theme === 'dark' ? '☀️' : '🌙'}
    </button>
  )
}

export function OfficeHeaderStats() {
  const tasks = useSyncExternalStore(subscribeSprintData, getTasksSnapshot)
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const meta = useSyncExternalStore(subscribeSprintData, getSprintMetaSnapshot)
  const showCurrent = running && !viewedSprint
  // 定时器驱动重渲染，让 AI 员工卡实时反映场景里的真实状态
  const [, forceTick] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => forceTick((n) => n + 1), 800)
    return () => clearInterval(timer)
  }, [])

  const total = showCurrent ? tasks.length : 0
  const done = showCurrent ? tasks.filter((task) => task.status === '通过').length : 0
  const pending = showCurrent ? tasks.filter((task) => !['通过', '已取消'].includes(task.status)).length : 0

  const agents = getOfficeAgents()
  const busy = agents.filter(
    (agent) => agent.state !== 'idle' && agent.state !== 'walking',
  ).length

  const stats = [
    {
      label: '本轮任务',
      value: String(total),
      hint: running ? 'Sprint 进行中' : '等待新任务',
      online: false,
    },
    {
      label: '已完成',
      value: String(done),
      hint: '本轮 Sprint',
      online: false,
    },
    {
      label: running ? '待处理' : '未完成',
      value: String(pending),
      hint: running ? (pending > 0 ? '开发/测试中' : '暂无阻塞') : '本轮已结束',
      online: false,
    },
    {
      label: 'AI 员工',
      value: `${busy}/${agents.length || 6}`,
      hint: busy > 0 ? `${busy} 人工作中` : '全员待命',
      online: true,
    },
  ]

  return (
    <>
      <header className="office-header-stats">
        {showCurrent ? stats.map((stat) => (
          <section key={stat.label} className="stat-card">
            <span className="stat-label">{stat.label}</span>
            <strong className="stat-value">{stat.value}</strong>
            <span className={`stat-hint ${stat.online ? 'online-hint' : ''}`}>
              {stat.online ? <span className="agent-dot online" /> : null}
              {stat.hint}
            </span>
          </section>
        )) : (
          <section className="stat-card idle-stat-card">
            <span className="stat-label">当前状态</span>
            <strong className="stat-value">{viewedSprint ? '正在查看历史记录' : '暂无进行中的任务'}</strong>
            <span className="stat-hint">{viewedSprint ? '切回当前 Sprint 可查看实时状态' : '上轮任务可在「任务」中查看'}</span>
          </section>
        )}
        <OfficeSystemResources />
        <ThemeToggle />
        {meta.id && showCurrent ? <SprintProgress meta={meta} running={running} /> : null}
      </header>
      <SprintHistoryPicker />
    </>
  )
}

/** 全局历史 Sprint 选择器：切换后任务/产物/文件等所有页面跟随；支持删除历史记录 */
function SprintHistoryPicker() {
  const history = useSyncExternalStore(subscribeView, getSprintHistory)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const [deleting, setDeleting] = useState(false)

  useEffect(() => {
    void refreshSprintHistory()
  }, [])

  if (history.length === 0) return null
  const current = history.find((item) => item.id === viewedSprint)

  async function removeSprint() {
    if (!current || deleting) return
    const ok = window.confirm(
      `确定删除 Sprint「${current.requirement.slice(0, 30) || current.id}」？\n` +
        `将一并删除其任务、产物、事件记录与磁盘文件（约 ${current.artifact_count} 个产物），不可恢复。`,
    )
    if (!ok) return
    setDeleting(true)
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/sprints/${current.id}`, {
        method: 'DELETE',
      })
      const detail = (await res.json().catch(() => null)) as { detail?: string } | null
      if (!res.ok) {
        showToast(detail?.detail ?? '删除失败')
        return
      }
      showToast('历史 Sprint 已删除')
      await setViewedSprint('')
      await refreshSprintHistory()
    } catch {
      showToast('删除失败，请确认后端已启动')
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="sprint-picker-bar">
      <span className="sprint-picker-label">查看记录</span>
      <select
        value={viewedSprint}
        onChange={(event) => void setViewedSprint(event.target.value)}
      >
        <option value="">当前 Sprint（实时）</option>
        {history.map((item) => (
          <option key={item.id} value={item.id}>
            {new Date(item.started_at * 1000).toLocaleString()} ·{' '}
            {item.requirement.slice(0, 24) || '未命名'}（{item.artifact_count} 个产物
            {item.usage_cost ? `，¥${item.usage_cost.toFixed(2)}` : ''}）
          </option>
        ))}
      </select>
      {viewedSprint ? (
        <>
          <span className="sprint-picker-viewing">正在查看历史 Sprint（只读）</span>
          <button
            type="button"
            className="sprint-picker-btn"
            onClick={() => void setViewedSprint('')}
          >
            返回当前
          </button>
          <button
            type="button"
            className="sprint-picker-btn danger"
            disabled={deleting}
            onClick={() => void removeSprint()}
          >
            {deleting ? '删除中…' : '删除该 Sprint'}
          </button>
        </>
      ) : null}
    </div>
  )
}

const STAGE_PROGRESS: Record<string, number> = {
  准备开工: 5,
  需求拆解: 20,
  任务分派: 32,
  并行开发: 55,
  测试验证: 70,
  技术评审: 82,
  缺陷修复: 88,
  交付成品: 94,
  复盘收尾: 97,
  已完成: 100,
  已归档: 100,
  已手动结束: 100,
  已达预算上限: 100,
  交付待验证: 100,
  交付待修正: 100,
  直接生成成品: 100,
}

function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const m = Math.floor(total / 60)
  const s = total % 60
  return m > 0 ? `${m} 分 ${s} 秒` : `${s} 秒`
}

/** 顶部进度条：阶段进度 + 任务完成度（百分比）、已耗时、本次花费 */
function SprintProgress({
  meta,
  running,
}: {
  meta: ReturnType<typeof getSprintMetaSnapshot>
  running: boolean
}) {
  const tasks = useSyncExternalStore(subscribeSprintData, getTasksSnapshot)
  const [now, setNow] = useState(0)
  useEffect(() => {
    setNow(Date.now() / 1000)
    if (!running) return
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => window.clearInterval(timer)
  }, [running, meta.stage])

  const stageProgress = STAGE_PROGRESS[meta.stage] ?? 0
  const taskProgress = computeSprintProgress(tasks)
  const clock = now || meta.finishedAt || meta.startedAt
  const elapsed = (meta.finishedAt || clock) - (meta.startedAt || clock)
  const stageElapsed = meta.stageStarted ? clock - meta.stageStarted : 0

  return (
    <section className="sprint-progress">
      <div className="sprint-progress-head">
        <strong>{running ? meta.stage || '进行中' : '本轮已结束'}</strong>
        <span>
          总用时 {formatDuration(elapsed)}
          {running && meta.stageStarted
            ? ` · 本阶段 ${formatDuration(stageElapsed)}`
            : ''}
          {meta.fast ? ' · 极速模式' : ''}
        </span>
        <em>
          当前 Sprint 预计花费 ¥{meta.cost.toFixed(3)}
          {meta.budget > 0 ? ` / 上限 ¥${meta.budget.toFixed(2)}` : ''}
        </em>
      </div>
      <div className="sprint-progress-bar">
        <span style={{ width: `${stageProgress}%` }} />
      </div>
      <div className="sprint-task-progress">
        <div className="sprint-task-progress-head">
          <span>任务完成度</span>
          <strong>
            {taskProgress.hasTasks ? `${taskProgress.percent}%` : '—'}
          </strong>
          <em>
            {taskProgress.hasTasks
              ? `已完成 ${taskProgress.done}/${taskProgress.total}`
              : '暂无任务'}
          </em>
        </div>
        <div className="sprint-task-progress-bar">
          <span
            style={{ width: `${taskProgress.percent}%` }}
            className={taskProgress.percent >= 100 ? 'is-done' : ''}
          />
        </div>
      </div>
    </section>
  )
}

const EMPTY_PANEL_STYLE = { padding: '12px 0', color: 'var(--text-muted, #888)' }

/** 任务详情弹窗：状态/负责人/产出说明/关联产物与失败原因一览；进行中任务可取消或改派 */
export function TaskDetailDialog({
  task,
  onClose,
}: {
  task: SprintTask
  onClose: () => void
}) {
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const meta = displayTaskMeta(task.status, running)
  const artifacts = useSyncExternalStore(subscribeArtifacts, getArtifactsSnapshot)
  const related = artifacts.filter((a) => a.task_id === task.id)
  const [agents, setAgents] = useState<AgentApiStatus[]>([])
  const [acting, setActing] = useState(false)

  // 任务可操作（未完成未取消）时拉取成员名册供改派选择
  const canIntervene = running && !['通过', '已取消'].includes(task.status)
  useEffect(() => {
    if (!canIntervene) return
    let cancelled = false
    fetch(`${OFFICE_API_BASE}/api/agents`)
      .then((res) => res.json())
      .then((payload: { agents?: AgentApiStatus[] }) => {
        if (!cancelled) setAgents(payload.agents ?? [])
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [canIntervene])

  async function cancelTask() {
    if (acting) return
    const ok = window.confirm(
      `确定取消任务「${task.title || task.id}」？取消后不再继续执行。`,
    )
    if (!ok) return
    setActing(true)
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/tasks/${task.id}/cancel`, {
        method: 'POST',
      })
      const detail = (await res.json().catch(() => null)) as {
        detail?: string
      } | null
      if (!res.ok) {
        showToast(detail?.detail ?? '取消任务失败')
        return
      }
      showToast('任务已取消')
      onClose()
    } catch {
      showToast('取消任务失败，请确认后端已启动')
    } finally {
      setActing(false)
    }
  }

  async function reassignTask(roster: number) {
    if (acting || !roster) return
    setActing(true)
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/tasks/${task.id}/reassign`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ roster }),
      })
      const detail = (await res.json().catch(() => null)) as {
        detail?: string
      } | null
      if (!res.ok) {
        showToast(detail?.detail ?? '改派失败')
        return
      }
      const target = agents.find((a) => a.roster === roster)
      showToast(`已改派给 ${target ? `${target.role}·${target.name}` : `#${roster}`}`)
      onClose()
    } catch {
      showToast('改派失败，请确认后端已启动')
    } finally {
      setActing(false)
    }
  }

  return createPortal(
    <div className="sprint-backdrop" onClick={onClose}>
      <div className="sprint-dialog" onClick={(e) => e.stopPropagation()}>
        <header className="sprint-dialog-head">
          <h3>
            {task.id} · {task.title || '未命名任务'}
          </h3>
          <button
            type="button"
            className="sprint-dialog-close"
            aria-label="关闭"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <div className="task-detail-body">
          <p>
            <span className={`agent-state-chip ${meta.done ? 'state-idle' : 'state-working'}`}>
              {meta.label}
            </span>{' '}
            负责人：{assigneeName(task.assignee_roster)} · 方向：{task.role_hint || '—'}
          </p>
          {canIntervene ? (
            <div className="task-intervene-row">
              <button
                type="button"
                className="task-cancel-btn"
                disabled={acting}
                onClick={() => void cancelTask()}
              >
                ✕ 取消任务
              </button>
              <select
                className="task-reassign-select"
                value=""
                disabled={acting || agents.length === 0}
                onChange={(event) => void reassignTask(Number(event.target.value))}
              >
                <option value="" disabled>
                  改派给…
                </option>
                {agents.map((agent) => (
                  <option key={agent.roster} value={agent.roster}>
                    {agent.role}·{agent.name}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          <h4>用户故事</h4>
          <p>{task.user_story || '历史任务未记录用户故事'}</p>
          <p>优先级：{task.priority ?? '未记录'} · 工作量：{task.story_points ?? '未记录'} 故事点</p>
          <h4>验收标准</h4>
          {task.acceptance_criteria?.length ? (
            <ul>{task.acceptance_criteria.map((criterion, index) => <li key={index}>{criterion}</li>)}</ul>
          ) : <p>历史任务未记录验收标准</p>}
          <h4>产出说明</h4>
          <p>{task.summary || '暂无说明（任务尚未完成或负责人未提交）'}</p>
          <h4>关联产物（{related.length}）</h4>
          {related.length === 0 ? (
            <p className="view-card-meta">暂无关联产物</p>
          ) : (
            <ul className="task-detail-artifacts">
              {related.map((a) => (
                <li key={a.id}>
                  <strong>{a.title}</strong>
                  {a.degraded ? (
                    <em className="artifact-degraded-tip">
                      ⚠ 兜底生成{a.degraded_reason ? `：${a.degraded_reason}` : ''}
                    </em>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
          <div className="sprint-dialog-actions">
            <button
              type="button"
              className="primary"
              onClick={() => {
                setActiveView('artifacts')
                onClose()
              }}
            >
              查看最终交付
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}

export function TaskCard({ task, onOpen }: { task: SprintTask; onOpen?: () => void }) {
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const meta = displayTaskMeta(task.status, running)
  const cancelled = task.status === '已取消'
  return (
    <article
      className="task-card task-card-clickable"
      role={onOpen ? 'button' : undefined}
      onClick={onOpen}
    >
      <div className="task-title-row">
        <strong className={cancelled ? 'task-title-cancelled' : undefined}>
          {task.title || task.id}
        </strong>
        <span
          title={!running && !['通过', '已取消'].includes(task.status) ? `原状态：${task.status}` : undefined}
          className={
            cancelled
              ? 'task-status task-status-cancelled'
              : meta.done
                ? 'task-status'
                : 'task-status active'
          }
        >
          {meta.label}
        </span>
      </div>
      <p>{assigneeName(task.assignee_roster)}</p>
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

export function OfficeRightPanel() {
  const tasks = useSyncExternalStore(subscribeSprintData, getTasksSnapshot)
  const activities = useSyncExternalStore(
    subscribeSprintData,
    getActivitiesSnapshot,
  )
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const showCurrent = running && !viewedSprint
  const activeWorkspace = useSyncExternalStore(
    subscribeView,
    getActiveWorkspace,
  )
  const [detailTask, setDetailTask] = useState<SprintTask | null>(null)

  const filteredTasks = tasks.filter((task) =>
    workspaceMatches(task.assignee_roster),
  )
  const workspaceLabel = WORKSPACES[activeWorkspace]?.label ?? ''

  return (
    <aside className="office-right-panel">
      <section className="panel-section">
        <h2>
          当前任务{showCurrent ? ' · 进行中' : ''}
          {activeWorkspace > 0 ? ` · ${workspaceLabel}` : ''}
        </h2>
        <div className="task-list">
          {!showCurrent ? (
            <div className="task-idle-state">
              <p>{viewedSprint ? '正在查看历史记录。' : '当前没有进行中的任务。'}</p>
              {tasks.length > 0 ? (
                <button type="button" onClick={() => setActiveView('tasks')}>查看任务记录</button>
              ) : null}
            </div>
          ) : filteredTasks.length === 0 ? (
            <p style={EMPTY_PANEL_STYLE}>
              {tasks.length === 0
                ? '暂无任务，发起 Sprint 后这里实时显示进展'
                : '当前筛选条件下没有任务，可切换到「全部」查看'}
            </p>
          ) : (
            filteredTasks.map((task) => (
              <TaskCard key={task.id} task={task} onOpen={() => setDetailTask(task)} />
            ))
          )}
        </div>
      </section>
      {detailTask ? (
        <TaskDetailDialog task={detailTask} onClose={() => setDetailTask(null)} />
      ) : null}

      <section className="panel-section panel-grow">
        <h2>实时动态</h2>
        {activities.length === 0 ? (
          <p style={EMPTY_PANEL_STYLE}>暂无动态，等待 Sprint 启动</p>
        ) : (
          <ul className="activity-list">
            {/* 只渲染最新 6 条，控制区块高度避免与快捷工具重叠 */}
            {activities.slice(-6).map((activity) => (
              <li key={activity.id}>
                <span className="activity-dot" />
                <p>
                  {activity.text}
                  <time>{activity.time}</time>
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="panel-section">
        <h2>快捷工具</h2>
        <div className="quick-tools">
          {QUICK_TOOLS.map((tool) => (
            <button
              key={tool.label}
              type="button"
              className="quick-tool"
              onClick={() => handleQuickTool(tool.label)}
            >
              <span>{tool.icon}</span>
              <strong>{tool.label}</strong>
            </button>
          ))}
        </div>
      </section>
    </aside>
  )
}

type LocationItem = { name: string; path: string }

async function startSprint(
  requirement: string,
  uploadIds: string[],
  fast = false,
  saveDir = '',
  budget = 0,
  taskKind = 'auto',
  executionMode = 'cloud',
  executionStyle = 'collaborative',
): Promise<string | null> {
  try {
    const res = await fetch(`${OFFICE_API_BASE}/api/sprint/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requirement, upload_ids: uploadIds, fast, save_dir: saveDir, budget, task_kind: taskKind, execution_mode: executionMode, execution_style: executionStyle }),
    })
    if (res.ok) return null
    const detail = await res.json().catch(() => null)
    return typeof detail?.detail === 'string' ? detail.detail : '启动失败'
  } catch {
    return '无法连接后端，请确认服务已启动'
  }
}

type AgentApiStatus = { roster: number; name: string; role: string; has_custom_key: boolean }
type SprintPlan = { route: string; output: string; attachments: OfficeUpload[]; save_dir: string; budget: number; data_notice: string }

/** 新建 Sprint 弹窗的示例需求：点击填入输入框 */
const SPRINT_EXAMPLES: { label: string; text: string }[] = [
  {
    label: '校园生活作文',
    text: '写一份关于「我的校园生活」的800字作文，生成Word文档',
  },
  {
    label: '团队项目周报',
    text: '生成一份团队项目周报Word文档，保存到桌面',
  },
  {
    label: 'AI 产品 PPT',
    text: '制作一份「AI产品介绍」PPT，8页',
  },
  {
    label: '待办 HTML 应用',
    text: '开发一个单文件HTML待办事项管理应用，可直接浏览器打开',
  },
]

function SprintDialog() {
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const [requirement, setRequirement] = useState(() => takeSprintPrefill())
  const [submitting, setSubmitting] = useState(false)
  const [demoStarting, setDemoStarting] = useState(false)
  const [attachments, setAttachments] = useState<OfficeUpload[]>([])
  const [fast, setFast] = useState(false)
  const [saveDir, setSaveDir] = useState('')
  const [customDir, setCustomDir] = useState('')
  const [locations, setLocations] = useState<LocationItem[]>([])
  const [budgetText, setBudgetText] = useState('')
  const [taskKind, setTaskKind] = useState<'auto' | 'file' | 'software'>('auto')
  const [executionMode, setExecutionMode] = useState<'cloud' | 'local'>('cloud')
  const [executionStyle, setExecutionStyle] = useState<'collaborative' | 'quick'>('collaborative')
  const [plan, setPlan] = useState<SprintPlan | null>(null)
  const [planSignature, setPlanSignature] = useState('')
  const [localReady, setLocalReady] = useState(false)
  const [agentStatus, setAgentStatus] = useState<AgentApiStatus[]>([])

  // 拉取真实系统目录（桌面/文档/下载等），供保存位置下拉选择
  useEffect(() => {
    async function load() {
      try {
        const res = await fetch(`${OFFICE_API_BASE}/api/locations`)
        if (!res.ok) return
        const payload = (await res.json()) as { locations?: LocationItem[] }
        setLocations(payload.locations ?? [])
      } catch {
        // 后端未启动时隐藏该选项即可
      }
      // 开工前 API 体检：列出未配置专属 Key 的成员，避免静默 Mock
      try {
        const res = await fetch(`${OFFICE_API_BASE}/api/agents`)
        if (!res.ok) return
        const payload = (await res.json()) as { agents?: AgentApiStatus[] }
        setAgentStatus(payload.agents ?? [])
      } catch {
        // 体检失败不阻断开工
      }
      try {
        const response = await fetch(`${OFFICE_API_BASE}/api/privacy/capabilities`)
        const result = await response.json() as { local_model_ready?: boolean }
        setLocalReady(Boolean(result.local_model_ready))
      } catch { setLocalReady(false) }
    }
    void load()
  }, [])

  const mockAgents = agentStatus.filter((a) => !a.has_custom_key)

  async function handleSubmit() {
    const text = requirement.trim()
    if (!text) {
      showToast('请先描述需求')
      return
    }
    const budget = budgetText.trim() ? Number(budgetText) : 0
    if (budgetText.trim() && (!Number.isFinite(budget) || budget < 0)) {
      showToast('预算上限请输入非负数字（元），留空表示不限')
      return
    }
    // 自定义路径：select 选「自定义」时以手填路径为准
    const effectiveSaveDir = saveDir === '__custom__' ? customDir.trim() : saveDir
    if (saveDir === '__custom__' && !effectiveSaveDir) {
      showToast('请填写自定义保存路径，或改选其他保存位置')
      return
    }
    const payload = { requirement: text, upload_ids: attachments.map(item => item.id), fast, save_dir: effectiveSaveDir, budget, task_kind: taskKind, execution_mode: executionMode, execution_style: executionStyle }
    const signature = JSON.stringify(payload)
    if (!plan || planSignature !== signature) {
      setSubmitting(true)
      try {
        const response = await fetch(`${OFFICE_API_BASE}/api/sprint/plan`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: signature })
        const result = await response.json() as SprintPlan & { detail?: string }
        if (!response.ok) { showToast(result.detail || '计划生成失败'); return }
        setPlan(result)
        setPlanSignature(signature)
      } catch { showToast('计划生成失败，请确认后端已启动') }
      finally { setSubmitting(false) }
      return
    }
    requestNotifyPermission()
    setSubmitting(true)
    const error = await startSprint(
      text,
      attachments.map((item) => item.id),
      fast,
      effectiveSaveDir,
      budget,
      taskKind,
      executionMode,
      executionStyle,
    )
    setSubmitting(false)
    if (error) {
      showToast(error)
      return
    }
    showToast('任务已启动，可在「任务」查看进度')
    closeSprintDialog()
  }

  /** 演示模式：脚本化 Sprint，零 API 消耗，无需任何 Key */
  async function handleDemo() {
    requestNotifyPermission()
    setDemoStarting(true)
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/demo/start`, {
        method: 'POST',
      })
      if (res.status === 409) {
        showToast('已有 Sprint 进行中')
        return
      }
      if (!res.ok) {
        const detail = (await res.json().catch(() => null)) as {
          detail?: string
        } | null
        showToast(detail?.detail ?? '演示启动失败')
        return
      }
      showToast('🎬 演示 Sprint 已启动（零 API 消耗），看办公室演出吧')
      closeSprintDialog()
    } catch {
      showToast('无法连接后端，请确认服务已启动')
    } finally {
      setDemoStarting(false)
    }
  }

  const currentSignature = JSON.stringify({
    requirement: requirement.trim(),
    upload_ids: attachments.map((item) => item.id),
    fast,
    save_dir: saveDir === '__custom__' ? customDir.trim() : saveDir,
    budget: budgetText.trim() ? Number(budgetText) : 0,
    task_kind: taskKind,
    execution_mode: executionMode,
    execution_style: executionStyle,
  })
  const planReady = plan !== null && planSignature === currentSignature

  // Portal 挂到 body：避开底部工具栏的 transform，
  // 否则 fixed 遮罩会被裁剪在工具栏区域内导致弹窗显示不全
  return createPortal(
    <div className="sprint-backdrop" onClick={closeSprintDialog}>
      <div className="sprint-dialog" onClick={(e) => e.stopPropagation()}>
        <header className="sprint-dialog-head">
          <h3>{planReady ? '确认执行计划' : '交代一件事'}</h3>
          <button
            type="button"
            className="sprint-dialog-close"
            aria-label="关闭"
            onClick={closeSprintDialog}
          >
            ×
          </button>
        </header>
        {!planReady ? (
          <>
        <label className="sprint-input-label" htmlFor="sprint-requirement">你想完成什么？</label>
        <textarea
          id="sprint-requirement"
          autoFocus
          placeholder="说出你想完成的事，例如：根据附件写一份项目总结 Word"
          value={requirement}
          onChange={(e) => setRequirement(e.target.value)}
        />
        <span className="sprint-dialog-tip">只需描述结果，例如“根据附件写一份项目总结 Word”。稍后可以先核对执行计划。</span>
        <div className="sprint-example-chips">
          <span className="sprint-dialog-tip">没有头绪？试试：</span>
          {SPRINT_EXAMPLES.map((example) => (
            <button
              key={example.label}
              type="button"
              title={example.text}
              onClick={() => setRequirement(example.text)}
            >
              {example.label}
            </button>
          ))}
        </div>
        <div className="sprint-attachment-row">
          <span>相关资料（可选）</span>
          <AttachmentPicker value={attachments} onChange={setAttachments} />
        </div>
        <p className="sprint-privacy-note">
          {executionMode === 'cloud'
            ? '任务内容与附件会发送到已配置的模型 API；预览计划不会启动模型任务。'
            : '任务仅调用本机模型 API；预览计划不会启动模型任务。'}
        </p>
        {mockAgents.length > 0 ? (
          <p className="sprint-setup-note">{mockAgents.length} 位成员尚未配置专属 API，可能使用模板内容。可在下方“更多设置”中查看。</p>
        ) : null}
        <details className="sprint-advanced">
          <summary>更多设置 <span>任务类型、模型、保存位置与预算</span></summary>
        <label className="sprint-fast-toggle"><span>任务类型：</span><select value={taskKind} onChange={event => {
          const nextKind = event.target.value as 'auto' | 'file' | 'software'
          setTaskKind(nextKind)
          if (nextKind === 'software') setExecutionStyle('collaborative')
        }}>
          <option value="auto">自动判断</option><option value="file">处理资料并生成文件</option><option value="software">开发软件项目</option>
        </select></label>
        <label className="sprint-fast-toggle"><span>生成流程：</span><select value={executionStyle} onChange={event => setExecutionStyle(event.target.value as 'collaborative' | 'quick')}>
          <option value="collaborative">六智能体协作（默认）</option>
          <option value="quick" disabled={taskKind === 'software'}>快速生成文件（不经过六人协作）</option>
        </select></label>
        {executionStyle === 'quick' ? <p className="sprint-dialog-tip">快速模式由单名成员直接生成与检查文件，不展示六人协作记录。</p> : null}
        {agentStatus.length > 0 && mockAgents.length > 0 ? (
          <p className="sprint-api-warning">
            ⚠ 以下成员未配置专属 API，将使用 Mock 兜底（产出为模板内容）：
            {mockAgents.map((a) => `${a.role}·${a.name}`).join('、')}
            。可在「团队 → ⚙ 设置」中补齐。
          </p>
        ) : null}
        <label className="sprint-fast-toggle"><span>模型模式：</span><select value={executionMode} onChange={event => setExecutionMode(event.target.value as 'cloud' | 'local')}>
          <option value="cloud">已配置模型 API（附件内容会发送到该 API）</option>
          <option value="local" disabled={!localReady}>仅本机模型 API{localReady ? '' : '（需先配置）'}</option>
        </select></label>
        <p className="sprint-dialog-tip">附件上传与资料库搜索在本机处理。执行模型任务时，只有本机模式限制请求发送到本机地址；请确认本机模型服务自身没有转发内容。</p>
        {locations.length > 0 ? (
          <label className="sprint-fast-toggle">
            <span>保存位置：</span>
            <select
              className="sprint-save-select"
              value={saveDir}
              onChange={(event) => setSaveDir(event.target.value)}
            >
              <option value="">不指定（跟随需求描述）</option>
              {locations.map((loc) => (
                <option key={loc.path} value={loc.path}>
                  {loc.name}（{loc.path}）
                </option>
              ))}
              <option value="__custom__">自定义路径…</option>
            </select>
          </label>
        ) : null}
        {saveDir === '__custom__' ? (
          <label className="sprint-fast-toggle">
            <span>自定义路径：</span>
            <input
              className="sprint-budget-input sprint-custom-dir-input"
              type="text"
              placeholder="例如 D:\输出\周报 或 ~/Documents/成品"
              value={customDir}
              onChange={(event) => setCustomDir(event.target.value)}
            />
          </label>
        ) : null}
        <label className="sprint-fast-toggle">
          <input
            type="checkbox"
            checked={fast}
            onChange={(event) => setFast(event.target.checked)}
          />
          <span>极速模式：跳过办公室演出动画，更快拿到产物</span>
        </label>
        <label className="sprint-fast-toggle">
          <span>花费上限（元）：</span>
          <input
            className="sprint-budget-input"
            type="number"
            min="0"
            step="0.1"
            placeholder="留空不限"
            value={budgetText}
            onChange={(event) => setBudgetText(event.target.value)}
          />
          <span className="sprint-dialog-tip">超过上限自动收尾，不再继续调用模型</span>
        </label>
        </details>
          </>
        ) : null}
        {planReady ? (
          <section className="sprint-plan" aria-label="执行计划预览">
            <strong>执行前请核对</strong>
            <dl>
              <div><dt>处理方式</dt><dd>{plan.route}</dd></div>
              <div><dt>任务内容</dt><dd>{requirement.trim()}</dd></div>
              <div><dt>保存位置</dt><dd>{plan.save_dir}</dd></div>
              <div><dt>输入资料</dt><dd>{plan.attachments.length ? plan.attachments.map((item) => item.name).join('、') : '仅需求文字'}</dd></div>
              <div><dt>花费上限</dt><dd>{plan.budget > 0 ? `¥${plan.budget.toFixed(2)}` : '未设置'}</dd></div>
            </dl>
            <p className="sprint-plan-notice">{executionMode === 'cloud' ? '确认启动后，任务内容与附件会发送到已配置的模型 API。' : plan.data_notice}</p>
          </section>
        ) : null}
        <div className="sprint-dialog-actions">
          {planReady ? (
            <button type="button" className="sprint-back-btn" onClick={() => { setPlan(null); setPlanSignature('') }}>返回修改</button>
          ) : (
          <button
            type="button"
            className="demo-btn"
            disabled={running || submitting || demoStarting}
            title="脚本化演示 Sprint，不调用任何模型 API"
            onClick={() => void handleDemo()}
          >
            {demoStarting ? '启动中…' : '先看演示（零 API 消耗）'}
          </button>
          )}
          <button
            type="button"
            className="primary"
            disabled={running || submitting || demoStarting || !requirement.trim()}
            onClick={handleSubmit}
          >
            {submitting ? '处理中…' : planReady ? '确认并启动' : '下一步：预览计划'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function downloadDataUrl(dataUrl: string, filename: string) {
  const anchor = document.createElement('a')
  anchor.href = dataUrl
  anchor.download = filename
  anchor.click()
}

function handleQuickTool(label: string) {
  switch (label) {
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
    case '语音输入':
      if (!isVoiceSupported()) {
        showToast('当前浏览器不支持语音识别，请改用 Chrome/Edge')
        return
      }
      showToast('正在聆听…请说出新任务需求')
      startVoiceRecognition()
        .then((text) => {
          openSprintDialog(text)
          showToast('语音识别完成，请确认内容后启动 Sprint')
        })
        .catch((error: Error) => showToast(error.message))
      return
  }
}

function exportDailyReport() {
  const tasks = getTasksSnapshot()
  const activities = getActivitiesSnapshot()
  if (tasks.length === 0) {
    showToast('暂无可导出的任务数据')
    return
  }

  const lines = [
    '# Sprint 日报',
    '',
    `- 导出时间：${new Date().toLocaleString()}`,
    '',
    '## 任务清单',
    ...tasks.map(
      (task) =>
        `- [${task.status === '通过' ? 'x' : ' '}] ${task.id} ${task.title}（${task.status}${
          task.assignee_roster ? '，' + assigneeName(task.assignee_roster) : ''
        }）`,
    ),
    '',
    '## 关键动态',
    ...activities.map((activity) => `- ${activity.time} ${activity.text}`),
    '',
  ]
  const blob = new Blob([lines.join('\n')], {
    type: 'text/markdown;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `sprint-report-${new Date().toISOString().slice(0, 10)}.md`
  anchor.click()
  URL.revokeObjectURL(url)
  showToast('日报已导出（Markdown）')
}

export function OfficeBottomToolbar() {
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const dialogOpen = useSyncExternalStore(subscribeView, isSprintDialogOpen)
  const [instructText, setInstructText] = useState('')
  const [instructing, setInstructing] = useState(false)
  const [mobilePaused, setMobilePaused] = useState(false)
  const [mobileBusy, setMobileBusy] = useState(false)

  useEffect(() => {
    if (!running) { setMobilePaused(false); return }
    const refresh = () => { void fetch(`${OFFICE_API_BASE}/api/state`).then(response => response.json()).then((data: { paused?: boolean }) => setMobilePaused(Boolean(data.paused))).catch(() => undefined) }
    refresh()
    const timer = window.setInterval(refresh, 5000)
    return () => window.clearInterval(timer)
  }, [running])

  async function toggleMobilePause() {
    if (!running || mobileBusy) return
    setMobileBusy(true)
    try {
      const response = await fetch(`${OFFICE_API_BASE}/api/sprint/${mobilePaused ? 'resume' : 'pause'}`, { method: 'POST' })
      if (!response.ok) throw new Error('操作失败')
      setMobilePaused(!mobilePaused)
      showToast(mobilePaused ? '任务已继续' : '任务已暂停')
    } catch { showToast('操作失败，请检查与后端的连接') }
    finally { setMobileBusy(false) }
  }

  /** Sprint 运行中向团队补充指示（后端广播 instruction_added，全员可见） */
  async function sendInstruct() {
    const text = instructText.trim()
    if (!text || instructing) return
    setInstructing(true)
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/sprint/instruct`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      })
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { detail?: string }
        window.alert(data.detail || '下达指示失败')
        return
      }
      setInstructText('')
      showToast('📌 指示已下达，团队会结合新指示继续工作')
    } catch {
      window.alert('无法连接后端，请确认服务已启动')
    } finally {
      setInstructing(false)
    }
  }

  function handleToolbar(label: string) {
    switch (label) {
      case '新建任务':
        openSprintDialog()
        return
      case '安排会议':
        submitVisitAction({
          type: 'desk_visit_tour',
          visitor: 6,
          hosts: [1, 2, 3, 4, 5],
        })
        showToast('敏捷教练开始巡回组织站立会议')
        return
      case '全部暂停': {
        setOfficePaused(true)
        // 真暂停：后端工作流挂起，模型调用全部停止（不再产生费用）
        fetch(`${OFFICE_API_BASE}/api/sprint/pause`, { method: 'POST' })
          .then(async (res) => {
            if (!res.ok) {
              const data = await res.json().catch(() => ({}))
              showToast(data.detail || '暂停失败')
              return
            }
            showToast('已暂停：动画与模型调用全部冻结，不再产生费用')
          })
          .catch(() => showToast('无法连接后端，仅暂停了前端动画'))
        return
      }
      case '全部继续': {
        setOfficePaused(false)
        fetch(`${OFFICE_API_BASE}/api/sprint/resume`, { method: 'POST' })
          .then(() => showToast('全体员工已恢复工作'))
          .catch(() => showToast('无法连接后端，仅恢复了前端动画'))
        return
      }
      case '任务结束':
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
        return
      case '导出日报':
        exportDailyReport()
        return
    }
  }

  return (
    <div className="office-bottom-toolbar">
      <div className="mobile-task-controls">
        <button type="button" onClick={() => openSprintDialog()} disabled={running}>新建任务</button>
        <button type="button" onClick={() => setActiveView('tasks')}>查看进度</button>
        <button type="button" onClick={() => setActiveView('artifacts')}>查看产物</button>
        {running ? <button type="button" disabled={mobileBusy} onClick={() => void toggleMobilePause()}>{mobilePaused ? '继续任务' : '暂停任务'}</button> : null}
      </div>
      <div className="toolbar-inner">
        {TOOLBAR_ITEMS.map((item) => (
          <button
            key={item.label}
            type="button"
            className={`toolbar-btn ${item.primary ? 'primary' : ''}`}
            onClick={() => handleToolbar(item.label)}
          >
            <span className="toolbar-icon">{item.icon}</span>
            <span>{item.label}</span>
          </button>
        ))}
        {running ? (
          <div className="toolbar-instruct">
            <input
              value={instructText}
              placeholder="补充指示，例如：周报里加上本周风险…"
              disabled={instructing}
              onChange={(event) => setInstructText(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void sendInstruct()
              }}
            />
            <button
              type="button"
              disabled={instructing || !instructText.trim()}
              onClick={() => void sendInstruct()}
            >
              {instructing ? '下达中…' : '📌 下达指示'}
            </button>
          </div>
        ) : null}
      </div>
      {dialogOpen ? <SprintDialog /> : null}
    </div>
  )
}

export function OfficeToast() {
  const message = useSyncExternalStore(subscribeToast, getToastSnapshot)
  const kind = useSyncExternalStore(subscribeToast, getToastKind)
  if (!message) return null
  return <div className={`office-toast toast-${kind}`}>{message}</div>
}
