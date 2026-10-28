import { useSyncExternalStore } from 'react'
import {
  isSprintDialogOpen,
  openSprintDialog,
  setActiveView,
  subscribeView,
} from '@/store/officeViewStore'
import { useSprintPause } from './officeComposer'
import { SprintDialog } from './sprintDialog'

export function OfficeBottomToolbar() {
  const { running, paused, busy, togglePause } = useSprintPause()
  const dialogOpen = useSyncExternalStore(subscribeView, isSprintDialogOpen)

  return (
    <div className="office-bottom-toolbar">
      <div className="mobile-task-controls">
        <button type="button" onClick={() => openSprintDialog()} disabled={running}>新建任务</button>
        <button type="button" onClick={() => setActiveView('tasks')}>查看进度</button>
        <button type="button" onClick={() => setActiveView('artifacts')}>查看产物</button>
        {running ? <button type="button" disabled={busy} onClick={() => void togglePause()}>{paused ? '继续任务' : '暂停任务'}</button> : null}
      </div>
      {dialogOpen ? <SprintDialog /> : null}
    </div>
  )
}
