/** 对话中心 store：每个成员的聊天线程（localStorage 持久化，刷新不丢） */

import { useEffect } from 'react'

export type ChatMessage = {
  role: 'user' | 'assistant'
  content: string
  time: string
  via?: 'llm' | 'mock'
  model?: string
  /** 轻提示（小灰字）：如图片被模型忽略、已切换备用通道 */
  hint?: boolean
}

const STORAGE_KEY = 'office.chat.v1'

type Threads = Record<number, ChatMessage[]>

let threads: Threads = {}
let selectedRoster = 1
let initialized = false

// 空线程必须是同一个引用：useSyncExternalStore 的快照不稳定会触发无限重渲染
const EMPTY_THREAD: ChatMessage[] = []

const listeners = new Set<() => void>()
let pendingRosters: readonly number[] = []
const controllers = new Map<number, AbortController>()

export function getPendingRosters() { return pendingRosters }

export function beginChat(roster: number): AbortController | null {
  if (controllers.has(roster)) return null
  const controller = new AbortController()
  controllers.set(roster, controller)
  pendingRosters = [...controllers.keys()]
  notify()
  return controller
}

export function finishChat(roster: number) {
  controllers.delete(roster)
  pendingRosters = [...controllers.keys()]
  persist()
  notify()
}

export function abortChat(roster: number) { controllers.get(roster)?.abort() }

function restore() {
  if (initialized) return
  initialized = true
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw) as {
      threads?: Threads
      selectedRoster?: number
    }
    threads = parsed.threads ?? {}
    if (typeof parsed.selectedRoster === 'number') {
      selectedRoster = parsed.selectedRoster
    }
  } catch {
    // 本地缓存损坏时忽略，不影响使用
  }
}

function persist() {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ threads, selectedRoster }),
    )
  } catch {
    // 存储空间不足时忽略
  }
}

function notify() {
  for (const listener of listeners) listener()
}

export function subscribeChat(listener: () => void) {
  restore()
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getSelectedRoster(): number {
  restore()
  return selectedRoster
}

export function setSelectedRoster(roster: number) {
  selectedRoster = roster
  persist()
  notify()
}

export function getThreadSnapshot(roster: number): ChatMessage[] {
  restore()
  return threads[roster] ?? EMPTY_THREAD
}

function nowLabel(): string {
  const d = new Date()
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function append(
  roster: number,
  message: ChatMessage,
): void {
  const current = threads[roster] ?? []
  threads = { ...threads, [roster]: [...current, message].slice(-200) }
  persist()
  notify()
}

export function appendUserMessage(roster: number, content: string) {
  append(roster, { role: 'user', content, time: nowLabel() })
}

export function appendAssistantMessage(
  roster: number,
  content: string,
  via: 'llm' | 'mock',
  model?: string,
) {
  append(roster, { role: 'assistant', content, time: nowLabel(), via, model })
}

/** 追加一条轻提示（渲染为小灰字，不参与气泡/模型标注） */
export function appendHintMessage(roster: number, content: string) {
  append(roster, { role: 'assistant', content, time: nowLabel(), hint: true })
}

// 流式回复时 localStorage 写入做节流，避免每个 delta 都全量序列化
let persistTimer: number | undefined

function persistThrottled() {
  if (persistTimer !== undefined) return
  persistTimer = window.setTimeout(() => {
    persistTimer = undefined
    persist()
  }, 400)
}

/** 流式回复：就地更新线程最后一条助手消息（不存在或最后是用户消息则追加） */
export function updateLastAssistantMessage(
  roster: number,
  content: string,
  via: 'llm' | 'mock',
  model?: string,
) {
  restore()
  const current = threads[roster] ?? []
  const last = current[current.length - 1]
  // 轻提示不可被流式增量覆盖，必须新起一条助手消息
  if (!last || last.role !== 'assistant' || last.hint) {
    append(roster, { role: 'assistant', content, time: nowLabel(), via, model })
    return
  }
  threads = {
    ...threads,
    [roster]: [
      ...current.slice(0, -1),
      { ...last, content, via, model: model ?? last.model },
    ],
  }
  persistThrottled()
  notify()
}

export function clearThread(roster: number) {
  threads = { ...threads, [roster]: [] }
  persist()
  notify()
}

/** 供聊天页在挂载时触发一次恢复（避免首帧空列表抖动） */
export function useRestoreChat() {
  useEffect(() => {
    restore()
    notify()
  }, [])
}
