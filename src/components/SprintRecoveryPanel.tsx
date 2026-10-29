import { useEffect, useState, useSyncExternalStore } from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { isSprintRunning, subscribeSprintData } from '@/store/officeTaskStore'
import { getViewedSprint, refreshSprintHistory, setViewedSprint, subscribeView } from '@/store/officeViewStore'
import { showToast } from '@/store/officeUiStore'

type RecoveryPlan = {
  sprint_id: string
  requirement: string
  resumable: boolean
  checkpoint_id: string
  next_steps: string[]
  spent: number
  budget: number
  warning: string
  reason: string
}

export function SprintRecoveryPanel() {
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const [plans, setPlans] = useState<RecoveryPlan[]>([])
  const [error, setError] = useState('')
  const [selected, setSelected] = useState('')
  const [accepted, setAccepted] = useState(false)
  const [busy, setBusy] = useState(false)
  const [refresh, setRefresh] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    if (running) return
    async function load() {
      try {
        const response = await fetch(`${OFFICE_API_BASE}/api/sprint/recoverable`, { signal: controller.signal })
        if (!response.ok) throw new Error('暂时无法读取续跑计划')
        const payload = await response.json() as { sprints: RecoveryPlan[] }
        if (!controller.signal.aborted) {
          setPlans(payload.sprints)
          setError('')
        }
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '读取失败')
      }
    }
    void load()
    return () => controller.abort()
  }, [running, viewedSprint, refresh])

  async function recover(plan: RecoveryPlan) {
    if (busy || !accepted) return
    setBusy(true)
    try {
      const response = await fetch(`${OFFICE_API_BASE}/api/sprints/${encodeURIComponent(plan.sprint_id)}/recover`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ checkpoint_id: plan.checkpoint_id, request_id: crypto.randomUUID() }),
      })
      const payload = await response.json() as { detail?: string }
      if (!response.ok) throw new Error(payload.detail || '续跑未启动')
      await setViewedSprint('')
      await refreshSprintHistory()
      setSelected('')
      setAccepted(false)
      showToast('已从保存的检查点继续执行')
    } catch (reason) {
      showToast(reason instanceof Error ? reason.message : '请求结果未确认，请刷新查看任务状态后重试')
      setRefresh(value => value + 1)
    } finally {
      setBusy(false)
    }
  }

  if (running) return null
  const visible = viewedSprint ? plans.filter(plan => plan.sprint_id === viewedSprint) : plans
  if (!visible.length && !error) return null
  return (
    <section className="view-card" aria-label="中断任务续跑">
      <h2>中断任务续跑</h2>
      {error ? <p role="alert">{error}</p> : null}
      <button type="button" className="view-ghost-btn" disabled={busy} onClick={() => setRefresh(value => value + 1)}>刷新续跑计划</button>
      {visible.map(plan => (
        <div key={plan.sprint_id} className="view-card">
          <strong>{plan.requirement}</strong>
          <p>{plan.resumable ? `继续步骤：${plan.next_steps.join(' → ')}` : plan.reason}</p>
          <p className="view-card-meta">已记账约 ¥{plan.spent.toFixed(4)} · {plan.budget ? `原预算 ¥${plan.budget.toFixed(2)}` : '原任务未设置预算上限'}</p>
          {plan.resumable ? <button type="button" className="view-ghost-btn" disabled={busy} onClick={() => { setSelected(plan.sprint_id); setAccepted(false) }}>查看并确认续跑</button> : null}
          {selected === plan.sprint_id && plan.resumable ? (
            <div>
              <p>{plan.warning}</p>
              <label><input type="checkbox" checked={accepted} disabled={busy} onChange={event => setAccepted(event.target.checked)} />我确认从上述步骤继续，沿用原任务预算与保存位置</label>
              <p><button type="button" className="view-primary-btn" disabled={!accepted || busy} onClick={() => void recover(plan)}>{busy ? '正在提交…' : '从检查点继续'}</button></p>
            </div>
          ) : null}
        </div>
      ))}
    </section>
  )
}
