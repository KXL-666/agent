/** Sprint 任务与动态的轻量响应式 store（配合 useSyncExternalStore 使用） */

import { AGENT_ROSTER } from '@/scene/layout/officeLayout'

export type SprintTask = {
  id: string
  title: string
  role_hint?: string
  assignee_roster?: number | null
  status: string
  summary?: string
}

export type SprintActivity = {
  id: string
  text: string
  time: string
}

const STATUS_META: Record<
  string,
  { label: string; tone: string; progress: number; done: boolean }
> = {
  待分派: { label: '等待开始', tone: 'purple', progress: 0, done: false },
  待开发: { label: '进行中', tone: 'blue', progress: 25, done: false },
  待测试: { label: '进行中', tone: 'blue', progress: 60, done: false },
  打回: { label: '打回修复', tone: 'orange', progress: 50, done: false },
  通过: { label: '已完成', tone: 'green', progress: 100, done: true },
  已取消: { label: '已取消', tone: 'gray', progress: 0, done: true },
}

export function statusMeta(status: string) {
  return (
    STATUS_META[status] ?? { label: status, tone: 'blue', progress: 50, done: false }
  )
}

export function assigneeName(rosterNo: number | null | undefined) {
  if (!rosterNo) return '未分配'
  return AGENT_ROSTER[rosterNo - 1]?.name ?? `#${rosterNo}`
}

const ACTIVITY_LIMIT = 8

export type SprintMetaState = {
  id: string
  requirement: string
  stage: string
  stageStarted: number
  startedAt: number
  finishedAt: number
  status: string
  fast: boolean
  cost: number
  /** 单轮花费上限（元），0 = 不限 */
  budget: number
}

let tasks: SprintTask[] = []
let activities: SprintActivity[] = []
let sprintRunning = false
let sprintPaused = false
let sprintMeta: SprintMetaState = {
  id: '',
  requirement: '',
  stage: '',
  stageStarted: 0,
  startedAt: 0,
  finishedAt: 0,
  status: 'idle',
  fast: false,
  cost: 0,
  budget: 0,
}

const listeners = new Set<() => void>()

function notify() {
  for (const listener of listeners) listener()
}

export function subscribeSprintData(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getTasksSnapshot(): SprintTask[] {
  return tasks
}

export function getActivitiesSnapshot(): SprintActivity[] {
  return activities
}

export function isSprintRunning(): boolean {
  return sprintRunning
}

export function getSprintMetaSnapshot(): SprintMetaState {
  return sprintMeta
}

export function applySprintMeta(patch: Partial<SprintMetaState>) {
  sprintMeta = { ...sprintMeta, ...patch }
  notify()
}

export function setSprintMetaCost(cost: number) {
  sprintMeta = { ...sprintMeta, cost }
  notify()
}

export function resetSprintMeta() {
  sprintMeta = {
    id: '',
    requirement: '',
    stage: '',
    stageStarted: 0,
    startedAt: 0,
    finishedAt: 0,
    status: 'idle',
    fast: false,
    cost: 0,
    budget: 0,
  }
  notify()
}

export function applyTaskSnapshot(next: SprintTask[]) {
  tasks = next.map((task) => ({ ...task }))
  notify()
}

export function upsertTask(task: SprintTask) {
  if (!task.id) return

  const index = tasks.findIndex((item) => item.id === task.id)
  if (index >= 0) {
    tasks = tasks.map((item, i) => (i === index ? { ...item, ...task } : item))
  } else {
    tasks = [...tasks, { ...task }]
  }
  notify()
}

export function setSprintRunning(running: boolean) {
  sprintRunning = running
  if (!running) sprintPaused = false
  notify()
}

export function isSprintPaused(): boolean {
  return sprintPaused
}

export function setSprintPaused(paused: boolean) {
  if (sprintPaused === paused) return
  sprintPaused = paused
  notify()
}

export function pushActivity(text: string) {
  const now = new Date()
  const time = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`
  activities = [
    {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      text,
      time,
    },
    ...activities,
  ].slice(0, ACTIVITY_LIMIT)
  notify()
}
