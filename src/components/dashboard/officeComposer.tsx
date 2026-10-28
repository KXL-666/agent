import { useEffect, useState, useSyncExternalStore } from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { setOfficePaused } from '@/scene/officeSceneBridge'
import { isSprintRunning, subscribeSprintData } from '@/store/officeTaskStore'
import { showToast } from '@/store/officeUiStore'

/**
 * 暂停/继续状态：轮询后端 /api/state 保持同步，任务停止后自动复位。
 * 供底部工具栏（移动端暂停按钮）与「对话」页共用。
 */
export function useSprintPause() {
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const [paused, setPaused] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!running) { setPaused(false); return }
    const refresh = () => { void fetch(`${OFFICE_API_BASE}/api/state`).then(response => response.json()).then((data: { paused?: boolean }) => setPaused(Boolean(data.paused))).catch(() => undefined) }
    void refresh()
    const timer = window.setInterval(refresh, 5000)
    return () => window.clearInterval(timer)
  }, [running])

  /** 暂停/继续：后端工作流 + 前端场景动画同时冻结/恢复 */
  async function togglePause() {
    if (!running || busy) return
    setBusy(true)
    try {
      const next = !paused
      const response = await fetch(`${OFFICE_API_BASE}/api/sprint/${next ? 'pause' : 'resume'}`, { method: 'POST' })
      if (!response.ok) throw new Error('操作失败')
      setPaused(next)
      setOfficePaused(next)
      showToast(next ? '任务已暂停：动画与模型调用全部冻结' : '全体员工已恢复工作')
    } catch { showToast('操作失败，请检查与后端的连接') }
    finally { setBusy(false) }
  }

  return { running, paused, busy, togglePause }
}
