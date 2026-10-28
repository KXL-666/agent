import { useSyncExternalStore } from 'react'
import {
  getNotificationsSnapshot,
  subscribeNotifications,
} from '@/store/officeEventStore'
import {
  getTasksSnapshot,
  isSprintRunning,
  subscribeSprintData,
} from '@/store/officeTaskStore'

export function CalendarView() {
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
          <h1>里程碑与动态</h1>
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
