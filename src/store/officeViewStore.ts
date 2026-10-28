/** 视图导航 / 工作空间 / 弹窗的轻量响应式 store（配合 useSyncExternalStore 使用） */

import { OFFICE_API_BASE } from '@/config/officeMode'
import { setArtifacts } from '@/store/officeArtifactStore'
import { applyTaskSnapshot, type SprintMetaState } from '@/store/officeTaskStore'
import { showToast } from '@/store/officeUiStore'

export type OfficeView =
  | 'home'
  | 'talk'
  | 'tasks'
  | 'agents'
  | 'files'
  | 'knowledge'
  | 'artifacts'
  | 'skills'
  | 'browser'
  | 'chat'
  | 'calendar'
  | 'notifications'

export type Workspace = {
  label: string
  /** 该空间关注的名册序号；null 表示全部 */
  roles: readonly number[] | null
}

/** 任务负责人筛选：名册 1 产品、2/3 研发、4/5 测试评审、6 协调 */
export const WORKSPACES: Workspace[] = [
  { label: '全部', roles: null },
  { label: '产品', roles: [1] },
  { label: '研发', roles: [2, 3] },
  { label: '测试与评审', roles: [4, 5] },
  { label: '协调', roles: [6] },
]

let activeView: OfficeView = 'talk'
let activeWorkspace = 0
let searchOpen = false
let sprintDialogOpen = false
let sprintPrefill = ''

const listeners = new Set<() => void>()

function notify() {
  for (const listener of listeners) listener()
}

export function subscribeView(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getActiveView(): OfficeView {
  return activeView
}

export function setActiveView(view: OfficeView) {
  if (activeView === view) return
  activeView = view
  notify()
}

export function getActiveWorkspace(): number {
  return activeWorkspace
}

export function setActiveWorkspace(index: number) {
  if (activeWorkspace === index) return
  activeWorkspace = index
  notify()
}

/** 当前工作空间是否包含该名册序号的任务 */
export function workspaceMatches(rosterNo: number | null | undefined): boolean {
  const roles = WORKSPACES[activeWorkspace]?.roles
  if (!roles) return true
  if (!rosterNo) return false
  return roles.includes(rosterNo)
}

export function isSearchOpen(): boolean {
  return searchOpen
}

export function setSearchOpen(open: boolean) {
  if (searchOpen === open) return
  searchOpen = open
  notify()
}

export function isSprintDialogOpen(): boolean {
  return sprintDialogOpen
}

export function openSprintDialog(prefill = '') {
  sprintPrefill = prefill
  sprintDialogOpen = true
  notify()
}

export function closeSprintDialog() {
  sprintPrefill = ''
  sprintDialogOpen = false
  notify()
}

export function takeSprintPrefill(): string {
  const value = sprintPrefill
  sprintPrefill = ''
  return value
}

/* --------------------------- 全局历史 Sprint 切换 --------------------------- */

export type SprintHistoryEntry = {
  id: string
  requirement: string
  status: string
  stage: string
  started_at: number
  artifact_count: number
  usage_cost?: number
  usage_tokens?: number
  usage_calls?: number
}

/** 正在查看的 Sprint id：'' 表示当前 Sprint；切换后任务/产物 store 会同步替换 */
let viewedSprint = ''
/** 正在查看的历史 Sprint 元信息（阶段/耗时/花费），用于顶部还原那一轮的进度条 */
let viewedMeta: SprintMetaState | null = null
let sprintHistory: SprintHistoryEntry[] = []
/** 切换竞态令牌：慢响应返回时若已再次切换则丢弃 */
let switchToken = 0

export function getViewedSprint(): string {
  return viewedSprint
}

export function getViewedSprintMeta(): SprintMetaState | null {
  return viewedMeta
}

export function getSprintHistory(): SprintHistoryEntry[] {
  return sprintHistory
}

/** 拉取历史 Sprint 列表供全局选择器展示（失败静默，不影响当前页面） */
export async function refreshSprintHistory(): Promise<void> {
  try {
    const res = await fetch(`${OFFICE_API_BASE}/api/sprints`)
    if (!res.ok) return
    const payload = (await res.json()) as { sprints?: SprintHistoryEntry[] }
    sprintHistory = payload.sprints ?? []
    notify()
  } catch {
    /* 历史列表拉取失败不影响当前产物展示 */
  }
}

/**
 * 全局切换查看的 Sprint：把对应任务/产物写入全局 store，
 * 任务中心、文件中心、产物中心、右侧任务流等所有页面自动跟随。
 */
export async function setViewedSprint(id: string): Promise<void> {
  if (viewedSprint === id) return
  viewedSprint = id
  viewedMeta = null
  notify()
  const token = ++switchToken
  try {
    const url = id
      ? `${OFFICE_API_BASE}/api/sprints/${id}`
      : `${OFFICE_API_BASE}/api/files`
    const res = await fetch(url)
    if (!res.ok) throw new Error(String(res.status))
    const payload = (await res.json()) as {
      tasks?: Parameters<typeof applyTaskSnapshot>[0]
      artifacts?: Parameters<typeof setArtifacts>[0]
      sprint?: Record<string, unknown>
    }
    if (token !== switchToken) return
    applyTaskSnapshot(payload.tasks ?? [])
    setArtifacts(payload.artifacts ?? [])
    const sprint = payload.sprint
    if (id && sprint?.id) {
      viewedMeta = {
        id: String(sprint.id),
        requirement: String(sprint.requirement ?? ''),
        stage: String(sprint.stage ?? ''),
        stageStarted: Number(sprint.stage_started ?? 0) || 0,
        startedAt: Number(sprint.started_at ?? 0) || 0,
        finishedAt: Number(sprint.finished_at ?? 0) || 0,
        status: String(sprint.status ?? ''),
        fast: sprint.execution_style === 'fast',
        cost: Number(sprint.usage_cost ?? 0) || 0,
        budget: Number(sprint.budget ?? 0) || 0,
      }
      notify()
    }
  } catch {
    showToast('加载历史 Sprint 失败，请确认后端已启动')
  }
}

/** 重新加载当前查看的 Sprint（WebSocket 重连后调用，避免展示过期数据） */
export async function reloadViewedSprint(): Promise<void> {
  const current = viewedSprint
  viewedSprint = ''
  await setViewedSprint(current)
}
