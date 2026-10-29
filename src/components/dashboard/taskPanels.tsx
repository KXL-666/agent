import { useEffect, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { OFFICE_API_BASE } from '@/config/officeMode'
import {
  getArtifactsSnapshot,
  subscribeArtifacts,
} from '@/store/officeArtifactStore'
import {
  getThoughtsSnapshot,
  subscribeThoughts,
  thoughtsForTask,
} from '@/store/officeThoughtStore'
import {
  assigneeName,
  displayTaskMeta,
  getActivitiesSnapshot,
  getSprintMetaSnapshot,
  getTasksSnapshot,
  isSprintRunning,
  subscribeSprintData,
  type SprintTask,
} from '@/store/officeTaskStore'
import { showToast } from '@/store/officeUiStore'
import {
  WORKSPACES,
  getActiveWorkspace,
  getViewedSprint,
  setActiveView,
  subscribeView,
  workspaceMatches,
} from '@/store/officeViewStore'

export type AgentApiStatus = { roster: number; name: string; role: string; has_custom_key: boolean }

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
  const sprintMeta = useSyncExternalStore(subscribeSprintData, getSprintMetaSnapshot)
  const meta = displayTaskMeta(task.status, running)
  const artifacts = useSyncExternalStore(subscribeArtifacts, getArtifactsSnapshot)
  const related = artifacts.filter((a) => a.task_id === task.id)
  useSyncExternalStore(subscribeThoughts, getThoughtsSnapshot)
  const thoughts = thoughtsForTask(task.id, task.assignee_roster)
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
          <p>
            本轮累计花费：¥{sprintMeta.cost.toFixed(3)}
            {sprintMeta.budget > 0 ? ` / 上限 ¥${sprintMeta.budget.toFixed(2)}` : ''}
            <span className="view-card-meta">（整轮 Sprint 合计，按 tokens 估算）</span>
          </p>
          <h4>验收标准</h4>
          {task.acceptance_criteria?.length ? (
            <ul>{task.acceptance_criteria.map((criterion, index) => <li key={index}>{criterion}</li>)}</ul>
          ) : <p>历史任务未记录验收标准</p>}
          <h4>决策依据</h4>
          {thoughts.length === 0 ? (
            <p className="view-card-meta">暂无决策记录（历史任务或本轮未产生思考事件）</p>
          ) : (
            <ul className="task-detail-thoughts">
              {thoughts.map((item) => (
                <li key={item.id}>
                  <strong>{assigneeName(item.rosterNo)}</strong>
                  {item.phase ? <span className="view-card-meta"> · {item.phase}</span> : null}
                  <p>{item.thought}</p>
                </li>
              ))}
            </ul>
          )}
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
  const meta = useSyncExternalStore(subscribeSprintData, getSprintMetaSnapshot)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const activeWorkspace = useSyncExternalStore(
    subscribeView,
    getActiveWorkspace,
  )
  const [detailTask, setDetailTask] = useState<SprintTask | null>(null)

  const filteredTasks = tasks.filter((task) =>
    workspaceMatches(task.assignee_roster),
  )
  // 「当前任务」只展示进行中的轮次：任何结束的轮次（完成/手动结束/失败/预算）
  // 一律归档到「任务」视图与历史记录，绝不在当前面板残留
  const roundEnded = !running && !viewedSprint && tasks.length > 0
  const visibleTasks = running || viewedSprint ? filteredTasks : []
  const workspaceLabel = WORKSPACES[activeWorkspace]?.label ?? ''

  return (
    <aside className="office-right-panel">
      <section className="panel-section">
        <div className="panel-head">
          <h2>
            {viewedSprint
              ? '历史任务 · 只读'
              : `当前任务${running ? ' · 进行中' : ''}`}
            {activeWorkspace > 0 ? ` · ${workspaceLabel}` : ''}
          </h2>
          {meta.id && running ? (
            <span className="panel-cost" title="本轮 Sprint 累计花费（按 tokens 估算）">
              ¥{meta.cost.toFixed(3)}
              {meta.budget > 0 ? ` / ¥${meta.budget.toFixed(2)}` : ''}
            </span>
          ) : null}
        </div>
        <div className="task-list">
          {visibleTasks.length === 0 ? (
            <div className="task-idle-state">
              <p>
                {viewedSprint
                  ? '正在查看历史记录。'
                  : roundEnded
                    ? '上一轮已结束，结果与产物已归档。'
                    : tasks.length === 0
                      ? '当前没有进行中的任务。'
                      : '当前筛选条件下没有任务，可切换到「全部」查看。'}
              </p>
              {roundEnded ? (
                <button type="button" onClick={() => setActiveView('tasks')}>
                  查看任务记录
                </button>
              ) : null}
            </div>
          ) : (
            visibleTasks.map((task) => (
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
    </aside>
  )
}
