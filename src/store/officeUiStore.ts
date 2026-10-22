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
