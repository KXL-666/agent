/** 通知中心的轻量响应式 store：记录后端推送的关键事件，角标统计未读 */

export type OfficeNotification = {
  id: string
  kind: string
  text: string
  time: string
}

const NOTIFICATION_LIMIT = 100

let items: OfficeNotification[] = []
let unread = 0

const listeners = new Set<() => void>()

function notify() {
  for (const listener of listeners) listener()
}

function nowTime(): string {
  const now = new Date()
  return `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`
}

export function subscribeNotifications(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getNotificationsSnapshot(): OfficeNotification[] {
  return items
}

export function getUnreadSnapshot(): number {
  return unread
}

/** 通知一条事件（角标 +1；重复内容 3 秒内去重，避免刷屏） */
export function recordNotification(text: string, kind: string) {
  if (!text) return
  const last = items[0]
  if (last && last.text === text && Date.now() - Number(last.id.split('-')[0]) < 3000) {
    return
  }
  items = [
    {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      kind,
      text,
      time: nowTime(),
    },
    ...items,
  ].slice(0, NOTIFICATION_LIMIT)
  unread += 1
  notify()
}

/** 打开通知中心时清零未读 */
export function markNotificationsRead() {
  if (unread === 0) return
  unread = 0
  notify()
}

export function clearNotifications() {
  items = []
  unread = 0
  notify()
}

/** 刷新/重连后用后端持久化的事件恢复通知列表（不计未读，避免历史刷屏） */
export function restoreNotifications(entries: { text: string; kind: string; time: string }[]) {
  items = entries.slice(0, NOTIFICATION_LIMIT).map((entry, index) => ({
    id: `restored-${index}-${entry.time}`,
    kind: entry.kind,
    text: entry.text,
    time: entry.time,
  }))
  unread = 0
  notify()
}
