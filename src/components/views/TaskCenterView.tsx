import { useState, useSyncExternalStore } from 'react'
import { TaskDetailDialog } from '../OfficeDashboardChrome'
import {
  assigneeName,
  displayTaskMeta,
  getSprintMetaSnapshot,
  getTasksSnapshot,
  isSprintRunning,
  subscribeSprintData,
  type SprintTask,
} from '@/store/officeTaskStore'
import {
  WORKSPACES,
  getActiveWorkspace,
  getSprintHistory,
  getViewedSprint,
  openSprintDialog,
  setActiveWorkspace,
  setViewedSprint,
  subscribeView,
  workspaceMatches,
} from '@/store/officeViewStore'
import { STATUS_ORDER } from './officeViewUtils'

function WideTaskCard({ task, onOpen }: { task: SprintTask; onOpen?: () => void }) {
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const meta = displayTaskMeta(task.status, running)
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
            {task.priority ? ` · ${task.priority} · ${task.story_points ?? 3} 点` : ''}
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

export function TaskCenterView() {
  const tasks = useSyncExternalStore(subscribeSprintData, getTasksSnapshot)
  const sprint = useSyncExternalStore(subscribeSprintData, getSprintMetaSnapshot)
  const workspaceIndex = useSyncExternalStore(subscribeView, getActiveWorkspace)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const history = useSyncExternalStore(subscribeView, getSprintHistory)
  const [detailTask, setDetailTask] = useState<SprintTask | null>(null)
  const workspace = WORKSPACES[workspaceIndex]
  const filtered = tasks.filter((task) =>
    workspaceMatches(task.assignee_roster),
  )
  const active = filtered.filter((task) => task.status !== '已取消')
  const totalPoints = active.reduce((sum, task) => sum + (task.story_points ?? 0), 0)
  const donePoints = active.filter((task) => task.status === '通过').reduce((sum, task) => sum + (task.story_points ?? 0), 0)
  const estimated = active.length > 0 && active.every((task) => Number.isFinite(task.story_points) && (task.story_points ?? 0) > 0)
  const doneCount = active.filter((task) => task.status === '通过').length
  const columns = [...STATUS_ORDER, ...(filtered.some((task) => task.status === '已取消') ? ['已取消'] : [])]

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>任务</h1>
          <p>
            迭代计划 · 用户故事 · 测试验收 · 复盘改进
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
          正在查看历史记录（只读），实时进展请
          <button type="button" onClick={() => void setViewedSprint('')}>
            返回本轮任务
          </button>
        </div>
      ) : null}

      <div className="task-filter" role="group" aria-label="按负责人筛选任务">
        <span>负责人</span>
        {WORKSPACES.map((option, index) => (
          <button key={option.label} type="button" className={workspaceIndex === index ? 'active' : ''} aria-pressed={workspaceIndex === index} onClick={() => setActiveWorkspace(index)}>
            {option.label}
          </button>
        ))}
      </div>

      <section className="view-card agile-overview" aria-label="迭代目标与进度">
        <h2>本轮迭代目标</h2>
        <p>{(viewedSprint ? history.find((item) => item.id === viewedSprint)?.requirement : sprint.requirement) || '创建 Sprint 后，将需求转成有优先级、工作量与验收条件的迭代计划。'}</p>
        <div className="view-chips">
          {estimated ? (
            <>
              <span className="view-chip">计划工作量 <strong>{totalPoints} 点</strong></span>
              <span className="view-chip">已通过 <strong>{donePoints} 点</strong></span>
              <span className="view-chip">剩余工作量 <strong>{totalPoints - donePoints} 点</strong></span>
            </>
          ) : (
            <>
              <span className="view-chip">本轮任务 <strong>{active.length} 项</strong></span>
              <span className="view-chip">已通过 <strong>{doneCount} 项</strong></span>
              <span className="view-chip">剩余 <strong>{active.length - doneCount} 项</strong></span>
            </>
          )}
          <span className="view-chip">待修复 <strong>{active.filter((task) => task.status === '打回').length}</strong></span>
          {!estimated && active.length > 0 ? <span className="view-chip">未完整估点 <strong>{active.filter((task) => !task.story_points).length} 项</strong></span> : null}
        </div>
        <progress aria-label={estimated ? '已通过故事点比例' : '已通过任务比例'} max={estimated ? totalPoints : active.length || 1} value={estimated ? donePoints : doneCount} />
        <p className="view-card-meta">{estimated ? '故事点表示相对工作量；' : '任务未完整估点，当前按任务数量显示进度；'}仅通过测试与评审的任务计入完成。点击任务查看用户故事和验收条件。最终文件另有成品质检。</p>
      </section>

      <div className="view-chips">
        {STATUS_ORDER.map((status) => (
          <span key={status} className="view-chip">
            {status}
            <strong>{filtered.filter((t) => t.status === status).length}</strong>
          </span>
        ))}
      </div>

      {filtered.length === 0 ? (
        <div className="view-empty">
          {tasks.length === 0
            ? '还没有任务。点击「新建 Sprint」描述需求，产品经理会自动拆解。'
            : '当前筛选条件下没有任务，可切换到「全部」查看。'}
        </div>
      ) : (
        <div className="agile-board">
          {columns.map((status) => (
            <section className="agile-column" key={status} aria-label={status}>
              <h2>{status} <small>{filtered.filter((task) => task.status === status).length}</small></h2>
              {filtered.filter((task) => task.status === status).map((task) => (
                <WideTaskCard key={task.id} task={task} onOpen={() => setDetailTask(task)} />
              ))}
              {!filtered.some((task) => task.status === status) ? <p className="view-card-meta">暂无任务</p> : null}
            </section>
          ))}
        </div>
      )}
      {detailTask ? (
        <TaskDetailDialog task={detailTask} onClose={() => setDetailTask(null)} />
      ) : null}
    </div>
  )
}
