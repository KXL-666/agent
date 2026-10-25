/** 全局 Toast 提示的轻量 store（默认 2.6 秒自动消失；success 类更醒目、停留更久） */

export type ToastKind = 'info' | 'success'

let message = ''
let kind: ToastKind = 'info'
let timer: ReturnType<typeof setTimeout> | undefined

const listeners = new Set<() => void>()

function notify() {
  for (const listener of listeners) listener()
}

export function subscribeToast(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getToastSnapshot(): string {
  return message
}

export function getToastKind(): ToastKind {
  return kind
}

export function showToast(
  text: string,
  opts?: { kind?: ToastKind; durationMs?: number },
) {
  message = text
  kind = opts?.kind ?? 'info'
  notify()
  if (timer) clearTimeout(timer)
  timer = setTimeout(
    () => {
      message = ''
      notify()
    },
    opts?.durationMs ?? (kind === 'success' ? 6000 : 2600),
  )
}

/** 产物就绪脉冲：Sprint 收尾/成品落地时点亮左侧「交付」，6 秒后自动熄灭 */
let artifactPulseAt = 0
let artifactPulseTimer: ReturnType<typeof setTimeout> | null = null

export function getArtifactPulseAt(): number {
  return artifactPulseAt
}

export function pulseArtifacts() {
  artifactPulseAt = Date.now()
  if (artifactPulseTimer) clearTimeout(artifactPulseTimer)
  artifactPulseTimer = setTimeout(() => {
    artifactPulseTimer = null
    artifactPulseAt = 0
    notify()
  }, 6000)
  notify()
}

/** 用户已经点开「交付」看过：立即结束提醒，不再显示徽标 */
export function clearArtifactPulse() {
  if (artifactPulseTimer) {
    clearTimeout(artifactPulseTimer)
    artifactPulseTimer = null
  }
  if (!artifactPulseAt) return
  artifactPulseAt = 0
  notify()
}
