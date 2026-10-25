/**
 * HTTP 拜访命令调度模式
 * - queue：入队串行，任务稀疏时不丢
 * - skip：繁忙时丢弃新命令，任务密集时减负
 * - hybrid：繁忙丢弃 + 空闲串行消费（不积压繁忙期命令）
 */
export type OfficeDispatchMode = 'queue' | 'skip' | 'hybrid'

const dispatchRaw = import.meta.env.VITE_OFFICE_DISPATCH_MODE
const remoteOrigin = typeof window !== 'undefined' && !['localhost', '127.0.0.1'].includes(window.location.hostname)
  ? window.location.origin : null
const remoteWebSocket = remoteOrigin ? `${remoteOrigin.replace(/^http/, 'ws')}/ws/office` : null

/** LangGraph 后端 WebSocket 地址（实时动作事件） */
export const OFFICE_WS_URL =
  import.meta.env.VITE_OFFICE_WS_URL ?? remoteWebSocket ?? 'ws://localhost:8000/ws/office'

/** LangGraph 后端 REST 地址（发起 Sprint 等） */
export const OFFICE_API_BASE =
  import.meta.env.VITE_OFFICE_API_BASE ?? remoteOrigin ?? 'http://localhost:8000'

/** 是否开启场景内置的假演示工作流（接入真实后端后应保持关闭） */
export const OFFICE_AUTO_DEMO = import.meta.env.VITE_OFFICE_AUTO_DEMO === 'true'

export const OFFICE_DISPATCH_MODE: OfficeDispatchMode =
  dispatchRaw === 'skip' || dispatchRaw === 'hybrid'
    ? dispatchRaw
    : 'queue'
