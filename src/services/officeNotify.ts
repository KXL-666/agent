/** Sprint 结局提醒：页面在后台时标题闪烁 + 浏览器 Notification 通知 */

const FLASH_TITLES = {
  done: '✅ Sprint 已完成',
  error: '❌ Sprint 出错',
  budget: '💰 已达预算上限',
} as const

export type SprintOutcomeKind = keyof typeof FLASH_TITLES

let originalTitle = typeof document !== 'undefined' ? document.title : ''
let flashTimer: number | undefined
let focusBound = false

/** 恢复原标题并清除闪烁定时器 */
export function stopTitleFlash() {
  if (flashTimer !== undefined) {
    window.clearInterval(flashTimer)
    flashTimer = undefined
  }
  document.title = originalTitle
}

function startTitleFlash(text: string) {
  if (flashTimer !== undefined) return
  originalTitle = document.title || originalTitle
  let toggled = false
  flashTimer = window.setInterval(() => {
    document.title = toggled ? originalTitle : text
    toggled = !toggled
  }, 1000)
  if (!focusBound) {
    focusBound = true
    window.addEventListener('focus', stopTitleFlash)
  }
}

/** 用户首次发起 Sprint / 演示模式时调用，静默申请浏览器通知权限 */
export function requestNotifyPermission() {
  try {
    if ('Notification' in window && Notification.permission === 'default') {
      void Notification.requestPermission().catch(() => undefined)
    }
  } catch {
    /* 权限申请失败不影响主流程 */
  }
}

/** Sprint 结束类事件提醒：后台时闪烁标题，有权限时弹系统通知 */
export function notifySprintOutcome(kind: SprintOutcomeKind, requirement: string) {
  const title = FLASH_TITLES[kind]
  if (document.hidden) startTitleFlash(title)
  try {
    if ('Notification' in window && Notification.permission === 'granted') {
      new Notification(title, { body: requirement.slice(0, 50) })
    }
  } catch {
    /* 系统通知不可用时不阻断 */
  }
}
