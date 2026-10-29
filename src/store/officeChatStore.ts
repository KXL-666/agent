/** 对话中心 store：每个成员可有多条对话线程（localStorage 持久化，刷新不丢） */

import { useEffect } from 'react'
import type { OfficeArtifact } from './officeArtifactStore'

export type ChatMessage = {
  role: 'user' | 'assistant' | 'task'
  content: string
  time: string
  via?: 'llm' | 'mock'
  model?: string
  /** 轻提示（小灰字）：如图片被模型忽略、已切换备用通道 */
  hint?: boolean
  /** 单飞任务卡片：关联后端 task_id，随 WS 事件更新状态 */
  taskId?: string
  taskStatus?: 'running' | 'done' | 'failed'
  artifactId?: string
  artifacts?: OfficeArtifact[]
  /** 随消息发送的图片附件 id 列表，用于在对话里回显 */
  images?: string[]
}

export type ConversationMeta = {
  id: string
  title: string
  createdAt: number
  /** 发起本轮 Sprint 的对话：Sprint 卡片块只在该对话里展示，新建对话后是干净窗口 */
  sprintId?: string
}

const STORAGE_KEY = 'office.chat.v1'

/** 新对话默认名：首条用户消息发出后自动改名为消息摘要 */
const NEW_CONVERSATION_TITLE = '新对话'

let conversations: Record<number, ConversationMeta[]> = {}
let activeConvIds: Record<number, string> = {}
let messagesByConv: Record<string, ChatMessage[]> = {}
/** 每个对话对应的后端会话 ID（切回老对话时恢复上下文） */
let backendConvByConv: Record<string, string> = {}
let artifactTargets: Record<string, string> = {}
let selectedRoster = 1
let initialized = false

// 空线程必须是同一个引用：useSyncExternalStore 的快照不稳定会触发无限重渲染
const EMPTY_THREAD: ChatMessage[] = []
const EMPTY_CONVERSATIONS: ConversationMeta[] = []

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

/** 从首条用户消息提炼对话名（老对话在列表里以此识别） */
function deriveTitle(messages: ChatMessage[]): string {
  const firstUser = messages.find((m) => m.role === 'user' && m.content.trim())
  const source = (firstUser?.content ?? messages[0]?.content ?? '').trim()
  if (!source) return NEW_CONVERSATION_TITLE
  const singleLine = source.replace(/\s+/g, ' ')
  return singleLine.length > 20 ? `${singleLine.slice(0, 20)}…` : singleLine
}

/** 旧版（按 roster 直存线程）数据迁移为对话列表结构 */
function migrateLegacy(parsed: {
  threads?: Record<number, ChatMessage[]>
  conversationIds?: Record<number, string>
  artifactTargets?: Record<number, string>
}) {
  const rosters = new Set<number>([
    ...Object.keys(parsed.threads ?? {}).map(Number),
    ...Object.keys(parsed.conversationIds ?? {}).map(Number),
  ])
  for (const roster of rosters) {
    const messages = parsed.threads?.[roster] ?? []
    const meta: ConversationMeta = {
      id: crypto.randomUUID(),
      title: messages.length ? deriveTitle(messages) : NEW_CONVERSATION_TITLE,
      createdAt: Date.now(),
    }
    conversations[roster] = [meta]
    activeConvIds[roster] = meta.id
    messagesByConv[meta.id] = messages
    const backendId = parsed.conversationIds?.[roster]
    if (backendId) backendConvByConv[meta.id] = backendId
    const target = parsed.artifactTargets?.[roster]
    if (target) artifactTargets[meta.id] = target
  }
}

function restore() {
  if (initialized) return
  initialized = true
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw) as {
      version?: number
      conversations?: Record<number, ConversationMeta[]>
      activeConvIds?: Record<number, string>
      messagesByConv?: Record<string, ChatMessage[]>
      backendConvByConv?: Record<string, string>
      artifactTargets?: Record<string, string>
      selectedRoster?: number
      // 旧版字段
      threads?: Record<number, ChatMessage[]>
      conversationIds?: Record<number, string>
    }
    if (parsed.version === 2) {
      conversations = parsed.conversations ?? {}
      activeConvIds = parsed.activeConvIds ?? {}
      messagesByConv = parsed.messagesByConv ?? {}
      backendConvByConv = parsed.backendConvByConv ?? {}
      artifactTargets = (parsed.artifactTargets ?? {}) as Record<string, string>
    } else {
      migrateLegacy(parsed)
    }
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
      JSON.stringify({
        version: 2,
        conversations,
        activeConvIds,
        messagesByConv,
        backendConvByConv,
        artifactTargets,
        selectedRoster,
      }),
    )
  } catch {
    // 存储空间不足时忽略
  }
}

function notify() {
  sidebarDirty = true
  for (const listener of listeners) listener()
}

export function subscribeChat(listener: () => void) {
  restore()
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/** 确保该成员有一条进行中的对话，返回其 ID（幂等，引用稳定） */
function ensureActiveConvId(roster: number): string {
  restore()
  const existing = activeConvIds[roster]
  if (existing && (conversations[roster] ?? []).some((c) => c.id === existing)) {
    return existing
  }
  const meta: ConversationMeta = {
    id: crypto.randomUUID(),
    title: NEW_CONVERSATION_TITLE,
    createdAt: Date.now(),
  }
  conversations = { ...conversations, [roster]: [...(conversations[roster] ?? []), meta] }
  activeConvIds = { ...activeConvIds, [roster]: meta.id }
  sidebarDirty = true
  return meta.id
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
  return messagesByConv[ensureActiveConvId(roster)] ?? EMPTY_THREAD
}

/** 该成员的对话列表（新对话排前面） */
export function getConversationsSnapshot(roster: number): ConversationMeta[] {
  restore()
  return conversations[roster] ?? EMPTY_CONVERSATIONS
}

export function getActiveConversationId(roster: number): string {
  return ensureActiveConvId(roster)
}

/** 把 Sprint 归属到发起它的对话：Sprint 卡片块只在该对话展示 */
export function markConversationSprint(roster: number, convId: string, sprintId: string) {
  restore()
  if (!sprintId) return
  const list = conversations[roster] ?? []
  const meta = list.find((c) => c.id === convId)
  if (!meta || meta.sprintId === sprintId) return
  conversations = {
    ...conversations,
    [roster]: list.map((c) => (c.id === convId ? { ...c, sprintId } : c)),
  }
  persist()
  notify()
}

/** 新建对话：当前对话还空着时直接复用，不制造空壳 */
export function createConversation(roster: number): string {
  const currentId = ensureActiveConvId(roster)
  const currentMessages = messagesByConv[currentId] ?? []
  if (currentMessages.length === 0) return currentId
  const meta: ConversationMeta = {
    id: crypto.randomUUID(),
    title: NEW_CONVERSATION_TITLE,
    createdAt: Date.now(),
  }
  conversations = { ...conversations, [roster]: [...(conversations[roster] ?? []), meta] }
  activeConvIds = { ...activeConvIds, [roster]: meta.id }
  persist()
  notify()
  return meta.id
}

/** 切换到老对话（消息与后端会话上下文一并恢复） */
export function openConversation(roster: number, convId: string) {
  restore()
  if (activeConvIds[roster] === convId) return
  if (!(conversations[roster] ?? []).some((c) => c.id === convId)) return
  activeConvIds = { ...activeConvIds, [roster]: convId }
  persist()
  notify()
}

/** 删除对话：消息、后端会话上下文一并清除；删的是当前对话时切到最近一条 */
export function deleteConversation(roster: number, convId: string) {
  restore()
  const metas = conversations[roster] ?? []
  if (!metas.some((c) => c.id === convId)) return
  const remaining = metas.filter((c) => c.id !== convId)
  conversations = { ...conversations, [roster]: remaining }
  messagesByConv = Object.fromEntries(Object.entries(messagesByConv).filter(([id]) => id !== convId))
  backendConvByConv = Object.fromEntries(Object.entries(backendConvByConv).filter(([id]) => id !== convId))
  artifactTargets = Object.fromEntries(Object.entries(artifactTargets).filter(([id]) => id !== convId))
  if (activeConvIds[roster] === convId) {
    const next = { ...activeConvIds }
    if (remaining.length) next[roster] = remaining[remaining.length - 1].id
    else delete next[roster]
    activeConvIds = next
  }
  persist()
  notify()
}

export type SidebarConversation = {
  roster: number
  conv: ConversationMeta
  /** 是否为该成员当前进行中的对话 */
  active: boolean
}

// 侧边栏聚合列表缓存：useSyncExternalStore 要求快照引用稳定
let sidebarCache: SidebarConversation[] = []
let sidebarDirty = true

/** 侧边栏「历史对话」聚合：团队 + 六位成员，按创建时间倒序；空且非进行中的对话不列出 */
export function getSidebarConversations(): SidebarConversation[] {
  restore()
  if (!sidebarDirty) return sidebarCache
  sidebarDirty = false
  const items: SidebarConversation[] = []
  for (const [rosterKey, metas] of Object.entries(conversations)) {
    const roster = Number(rosterKey)
    for (const conv of metas) {
      const active = activeConvIds[roster] === conv.id
      if (!active && (messagesByConv[conv.id] ?? []).length === 0) continue
      items.push({ roster, conv, active })
    }
  }
  items.sort((a, b) => b.conv.createdAt - a.conv.createdAt)
  sidebarCache = items
  return sidebarCache
}

/** 只读查看当前对话 ID，不为未用过的成员凭空建对话（供侧边栏高亮用） */
export function peekActiveConversationId(roster: number): string {
  restore()
  return activeConvIds[roster] ?? ''
}

/** 跨全部对话收集已记录进消息流的产物 ID（避免切换对话后重复推送产物卡） */
export function getRecordedArtifactIds(roster: number): Set<string> {
  restore()
  const ids = new Set<string>()
  for (const meta of conversations[roster] ?? []) {
    for (const m of messagesByConv[meta.id] ?? []) {
      for (const a of m.artifacts ?? []) ids.add(a.id)
    }
  }
  return ids
}

function nowLabel(): string {
  const d = new Date()
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function append(
  roster: number,
  message: ChatMessage,
): void {
  const convId = ensureActiveConvId(roster)
  const current = messagesByConv[convId] ?? []
  messagesByConv = { ...messagesByConv, [convId]: [...current, message].slice(-200) }
  // 首条用户消息落定后，把「新对话」改名为消息摘要
  if (message.role === 'user') {
    const metas = conversations[roster] ?? []
    const index = metas.findIndex((c) => c.id === convId)
    if (index >= 0 && metas[index].title === NEW_CONVERSATION_TITLE) {
      const title = deriveTitle(messagesByConv[convId])
      conversations = {
        ...conversations,
        [roster]: [...metas.slice(0, index), { ...metas[index], title }, ...metas.slice(index + 1)],
      }
    }
  }
  persist()
  notify()
}

export function appendUserMessage(roster: number, content: string, images?: string[]) {
  append(roster, { role: 'user', content, time: nowLabel(), ...(images?.length ? { images } : {}) })
}

export function appendAssistantMessage(
  roster: number,
  content: string,
  via: 'llm' | 'mock',
  model?: string,
) {
  append(roster, { role: 'assistant', content, time: nowLabel(), via, model })
}

export function getConversationId(roster: number): string {
  const convId = ensureActiveConvId(roster)
  if (!backendConvByConv[convId]) {
    backendConvByConv = { ...backendConvByConv, [convId]: crypto.randomUUID() }
    persist()
  }
  return backendConvByConv[convId]
}

export function getArtifactTarget(roster: number): string {
  restore()
  return artifactTargets[ensureActiveConvId(roster)] ?? ''
}

export function setArtifactTarget(roster: number, id: string) {
  const convId = ensureActiveConvId(roster)
  artifactTargets = { ...artifactTargets, [convId]: id }
  persist()
  notify()
}

export function appendConversationResult(roster: number, content: string, artifacts: OfficeArtifact[] = []) {
  append(roster, { role: 'assistant', content, artifacts, time: nowLabel(), via: 'llm' })
}

/** 追加一条轻提示（渲染为小灰字，不参与气泡/模型标注） */
export function appendHintMessage(roster: number, content: string) {
  append(roster, { role: 'assistant', content, time: nowLabel(), hint: true })
}

/** 在指定对话里追加轻提示（不打扰当前激活对话） */
export function appendHintToConversation(roster: number, convId: string, content: string) {
  restore()
  if (!(conversations[roster] ?? []).some((c) => c.id === convId)) return
  const current = messagesByConv[convId] ?? []
  messagesByConv = {
    ...messagesByConv,
    [convId]: [...current, { role: 'assistant' as const, content, time: nowLabel(), hint: true }].slice(-200),
  }
  persist()
  notify()
}

/** 找到发起指定 Sprint 的对话，没有则返回空串 */
export function findConversationBySprint(roster: number, sprintId: string): string {
  restore()
  return (conversations[roster] ?? []).find((c) => c.sprintId === sprintId)?.id ?? ''
}

/** 追加单飞任务卡片（进行中），等待 WS 事件更新为完成/失败 */
export function appendTaskMessage(roster: number, title: string, taskId: string) {
  append(roster, {
    role: 'task',
    content: title,
    time: nowLabel(),
    taskId,
    taskStatus: 'running',
  })
}

/** 单飞任务完成/失败后更新卡片状态并挂上产物 */
export function updateTaskMessage(
  roster: number,
  taskId: string,
  status: 'done' | 'failed',
  artifactId?: string | null,
) {
  restore()
  const convId = ensureActiveConvId(roster)
  const current = messagesByConv[convId] ?? []
  const index = current.findIndex(
    (m) => m.role === 'task' && m.taskId === taskId,
  )
  if (index < 0) return
  messagesByConv = {
    ...messagesByConv,
    [convId]: [
      ...current.slice(0, index),
      {
        ...current[index],
        taskStatus: status,
        artifactId: artifactId ?? current[index].artifactId,
      },
      ...current.slice(index + 1),
    ],
  }
  persist()
  notify()
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
  const convId = ensureActiveConvId(roster)
  const current = messagesByConv[convId] ?? []
  const last = current[current.length - 1]
  // 轻提示不可被流式增量覆盖，必须新起一条助手消息
  if (!last || last.role !== 'assistant' || last.hint) {
    append(roster, { role: 'assistant', content, time: nowLabel(), via, model })
    return
  }
  messagesByConv = {
    ...messagesByConv,
    [convId]: [
      ...current.slice(0, -1),
      { ...last, content, via, model: model ?? last.model },
    ],
  }
  persistThrottled()
  notify()
}

/** 供聊天页在挂载时触发一次恢复（避免首帧空列表抖动） */
export function useRestoreChat() {
  useEffect(() => {
    restore()
    notify()
  }, [])
}
