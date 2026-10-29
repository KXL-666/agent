import { useEffect, useState, useSyncExternalStore } from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { getOfficeAgents } from '@/store/officeStore'
import {
  computeSprintProgress,
  getSprintMetaSnapshot,
  getTasksSnapshot,
  isSprintRunning,
  subscribeSprintData,
  weightedSprintProgress,
} from '@/store/officeTaskStore'
import { showToast } from '@/store/officeUiStore'
import {
  getSprintHistory,
  getViewedSprint,
  getViewedSprintMeta,
  refreshSprintHistory,
  setViewedSprint,
  subscribeView,
} from '@/store/officeViewStore'
import { getTheme, subscribeTheme, toggleTheme } from '@/store/officeThemeStore'

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
    void poll()
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

/** 阶段在整个 Sprint 里的位置：必须与后端 graph.py 的真实阶段名一致，未知阶段会退回 0%。 */
const STAGE_PROGRESS: Record<string, number> = {
  准备开工: 3,
  需求拆解: 12,
  任务分派: 22,
  并行开发: 45,
  测试验证: 66,
  技术评审: 80,
  定点返工: 88,
  形成候选成品: 94,
  复盘收尾: 97,
  发布或保留候选: 99,
  已完成: 100,
  已归档: 100,
  已手动结束: 100,
  已达预算上限: 100,
  交付待验证: 100,
  交付待修正: 100,
  直接生成成品: 100,
  // 兼容旧阶段名
  缺陷修复: 88,
  交付成品: 94,
}

function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const m = Math.floor(total / 60)
  const s = total % 60
  return m > 0 ? `${m} 分 ${s} 秒` : `${s} 秒`
}

/** 顶部进度条：整体进度（阶段位置 + 任务推进加权）、已耗时、本次花费 */
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
  const weighted = weightedSprintProgress(tasks)
  const passed = computeSprintProgress(tasks)
  const clock = now || meta.finishedAt || meta.startedAt
  const elapsed = (meta.finishedAt || clock) - (meta.startedAt || clock)
  const stageElapsed = meta.stageStarted ? clock - meta.stageStarted : 0
  const completed = !running && meta.status === 'completed'
  const allPassed = !passed.hasTasks || passed.done === passed.total
  // 运行中：阶段位置 40% + 任务推进 60%（按状态加权，进度能持续前进）；
  // 已结束：只看真正通过验收的任务数，与每张任务卡、已完成统计保持同一口径。
  const taskPercent = running ? weighted.percent : passed.percent
  const overall = running
    ? !weighted.hasTasks
      ? stageProgress
      : Math.round(stageProgress * 0.4 + weighted.percent * 0.6)
    : passed.hasTasks
      ? passed.percent
      : completed
        ? 100
        : stageProgress
  const outcome = running
    ? meta.stage || '进行中'
    : meta.status === 'completed'
      ? allPassed
        ? '本轮已完成'
        : '本轮已结束 · 验收未完成'
      : ({ failed: '本轮未通过验收', stopped: '本轮已手动结束', interrupted: '后端重启，本轮已中断',
           budget: '已达预算上限' }[meta.status] ?? '本轮已结束')
  const remaining = meta.budget > 0 ? Math.max(0, meta.budget - meta.cost) : 0
  const barHint = !passed.hasTasks && !weighted.hasTasks
    ? `整体 ${overall}%：当前处于「${outcome}」阶段，尚无任务明细`
    : running
      ? `整体 ${overall}% = 阶段「${outcome}」${stageProgress}% × 40% + 任务推进 ${weighted.percent}% × 60%`
      : `整体 ${overall}%：按通过验收的任务计（${passed.done}/${passed.total}），未验收/打回的任务不计入`

  return (
    <section className={`sprint-progress ${running ? '' : 'is-finished'}`}>
      <div className="sprint-progress-head">
        <strong>整体进度 {overall}%</strong>
        <span>
          {outcome}
          {` · 总用时 ${formatDuration(elapsed)}`}
          {running && meta.stageStarted
            ? ` · 本阶段 ${formatDuration(stageElapsed)}`
            : ''}
          {meta.fast ? ' · 极速模式' : ''}
        </span>
        <em title="按 tokens 估算，可在「用量」页查看明细">
          本轮花费 ¥{meta.cost.toFixed(3)}
          {meta.budget > 0 ? ` / 上限 ¥${meta.budget.toFixed(2)} · 余 ¥${remaining.toFixed(2)}` : ''}
        </em>
      </div>
      <div
        className="sprint-progress-bar"
        role="progressbar"
        aria-valuenow={overall}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="本轮整体进度"
        title={barHint}
      >
        <span style={{ width: `${overall}%` }} />
      </div>
      <div className="sprint-task-progress">
        <div className="sprint-task-progress-head">
          <span>{running ? '任务推进' : '验收通过率'}</span>
          <strong>{passed.hasTasks || weighted.hasTasks ? `${taskPercent}%` : '—'}</strong>
          <em>
            {weighted.hasTasks
              ? `通过 ${passed.done}/${passed.total}`
              : '暂无任务'}
            {weighted.cancelled ? ` · 已取消 ${weighted.cancelled}` : ''}
          </em>
        </div>
        <div className="sprint-task-progress-bar">
          <span
            style={{ width: `${taskPercent}%` }}
            className={taskPercent >= 100 ? 'is-done' : ''}
          />
        </div>
      </div>
    </section>
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
      `确定删除这条记录「${current.requirement.slice(0, 30) || current.id}」？\n` +
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
      showToast('历史记录已删除')
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
        <option value="">本轮任务（实时）</option>
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
          <span className="sprint-picker-viewing">正在查看历史记录（只读）</span>
          <button
            type="button"
            className="sprint-picker-btn"
            onClick={() => void setViewedSprint('')}
          >
            返回本轮
          </button>
          <button
            type="button"
            className="sprint-picker-btn danger"
            disabled={deleting}
            onClick={() => void removeSprint()}
          >
            {deleting ? '删除中…' : '删除这条记录'}
          </button>
        </>
      ) : null}
    </div>
  )
}

export function OfficeHeaderStats() {
  const tasks = useSyncExternalStore(subscribeSprintData, getTasksSnapshot)
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const meta = useSyncExternalStore(subscribeSprintData, getSprintMetaSnapshot)
  const history = useSyncExternalStore(subscribeView, getSprintHistory)
  const viewedMeta = useSyncExternalStore(subscribeView, getViewedSprintMeta)
  // 本轮 Sprint 结束后仍保留进度条与花费，避免一结束就全部消失。
  const hasCurrent = Boolean(meta.id) && !viewedSprint
  const lastSprint = history[0]
  // 定时器驱动重渲染，让 AI 员工卡实时反映场景里的真实状态
  const [, forceTick] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => forceTick((n) => n + 1), 800)
    return () => clearInterval(timer)
  }, [])

  const total = hasCurrent ? tasks.length : 0
  const done = hasCurrent ? tasks.filter((task) => task.status === '通过').length : 0
  const pending = hasCurrent ? tasks.filter((task) => !['通过', '已取消'].includes(task.status)).length : 0

  const agents = getOfficeAgents()
  const busy = agents.filter(
    (agent) => agent.state !== 'idle' && agent.state !== 'walking',
  ).length

  const stats = [
    {
      label: '本轮任务',
      value: String(total),
      hint: running
        ? `Sprint 进行中${busy > 0 ? ` · ${busy} 人工作中` : ' · 全员待命'}`
        : '本轮已结束',
      online: busy > 0,
    },
    {
      label: '已完成',
      value: String(done),
      hint: running ? '本轮 Sprint' : '本轮最终结果',
      online: false,
    },
    {
      label: running ? '待处理' : '未完成',
      value: String(pending),
      hint: running ? (pending > 0 ? '开发/测试中' : '暂无阻塞') : '本轮已结束',
      online: false,
    },
    {
      label: '本轮花费',
      value: `¥${meta.cost.toFixed(2)}`,
      hint: meta.budget > 0 ? `上限 ¥${meta.budget.toFixed(2)}` : '按 tokens 估算',
      online: false,
    },
  ]

  return (
    <>
      <header className="office-header-stats">
        {hasCurrent ? stats.map((stat) => (
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
            <span className="stat-hint">
              {viewedSprint
                ? '在顶部「查看记录」选择「当前 Sprint」即可切回实时状态'
                : lastSprint
                  ? `上一轮：${lastSprint.artifact_count} 个产物${lastSprint.usage_cost ? `，花费 ¥${lastSprint.usage_cost.toFixed(2)}` : ''}，可在顶部「查看记录」回看`
                  : '发起第一个 Sprint 后，这里会显示进度、完成度与花费'}
            </span>
          </section>
        )}
        <OfficeSystemResources />
        <ThemeToggle />
        {hasCurrent ? (
          <SprintProgress meta={meta} running={running} />
        ) : viewedMeta ? (
          <SprintProgress meta={viewedMeta} running={false} />
        ) : null}
      </header>
      <SprintHistoryPicker />
    </>
  )
}
