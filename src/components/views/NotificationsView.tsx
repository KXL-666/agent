import { useEffect, useSyncExternalStore } from 'react'
import {
  clearNotifications,
  getNotificationsSnapshot,
  markNotificationsRead,
  subscribeNotifications,
} from '@/store/officeEventStore'
import { showToast } from '@/store/officeUiStore'
import { KIND_LABELS } from './officeViewUtils'

export function NotificationsView() {
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
          <h1>通知</h1>
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
              {item.detail ? (
                <details className="notification-record">
                  <p className="notification-detail-preview">
                    <strong>{item.title || '详细信息'}：</strong>{item.detail.split('\n').slice(0, 4).join(' · ')}
                  </p>
                  <summary>展开完整详情</summary>
                  <pre>{item.detail}</pre>
                </details>
              ) : null}
              <time>{item.time}</time>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
