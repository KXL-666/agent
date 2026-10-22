/** 暗色主题 store：data-theme 属性 + localStorage 持久化，默认亮色 */

import { setNightMode } from '@/scene/officeSceneBridge'

const STORAGE_KEY = 'office-theme'

type Theme = 'light' | 'dark'

let theme: Theme = 'light'

const listeners = new Set<() => void>()

function apply() {
  if (theme === 'dark') {
    document.documentElement.dataset.theme = 'dark'
  } else {
    delete document.documentElement.dataset.theme
  }
  // 联动办公室场景：暗色 = 夜晚 + 灯光，亮色 = 白天
  setNightMode(theme === 'dark')
}

/** 根组件挂载时调用：读取本地偏好并应用 */
export function initTheme(): void {
  try {
    theme = localStorage.getItem(STORAGE_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    theme = 'light'
  }
  apply()
}

export function subscribeTheme(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getTheme(): Theme {
  return theme
}

export function toggleTheme(): void {
  theme = theme === 'dark' ? 'light' : 'dark'
  try {
    localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    // 隐私模式写不进去就只切当前会话
  }
  apply()
  for (const listener of listeners) listener()
}
