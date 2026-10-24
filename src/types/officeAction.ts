import type { AgentState } from '@/types/agent'

export type OfficeActionDeskVisit = {
  type: 'desk_visit'
  visitor: number
  host: number
  message: string
}

export type OfficeActionDeskVisitTour = {
  type: 'desk_visit_tour'
  visitor: number
  hosts: number[]
  message?: string
}

export type OfficeActionSetState = {
  type: 'set_state'
  state: AgentState
  task?: string
  /** 名册序号，从 1 开始 */
  rosterNo?: number
  agentId?: string
  /** 非 Sprint 的文件生成状态，可在上一轮结束后独立更新负责成员。 */
  source?: 'artifact_generation'
}

export type OfficeActionEmote = {
  type: 'emote'
  /** Spine 动画名，如 emotes/idea */
  animation: string
  label?: string
  rosterNo?: number
  agentId?: string
}

/** 任务面板数据事件 */
export type OfficeActionTaskUpdate = {
  type: 'task_update'
  task: Record<string, unknown>
}

export type OfficeArtifactPayload = {
  id: string
  kind: 'prd' | 'dev' | 'test' | 'review' | 'summary' | 'custom' | 'final'
  title: string
  author_roster: number | null
  task_id: string | null
  content: string
  file_name?: string | null
  created_at: number
  /** 内容来自兜底/Mock 或真实文件生成失败 */
  degraded?: boolean
  degraded_reason?: string
  parent_id?: string | null
  version?: number
  sprint_id?: string
}

export type SprintMetaPayload = {
  id: string
  requirement: string
  started_at: number
  finished_at: number
  status: string
  stage: string
  stage_started: number
  deliver_kind: string
  fast: boolean
}

export type OfficeActionArtifactNew = {
  type: 'artifact_new'
  artifact: OfficeArtifactPayload
}

export type OfficeActionArtifactUpdate = {
  type: 'artifact_update'
  artifact: OfficeArtifactPayload
}

export type OfficeActionStage = {
  type: 'stage'
  stage: string
  started_at: number
  stage_started: number
}

export type OfficeActionSprintUsage = {
  type: 'sprint_usage'
  cost: number
  tokens: number
}

export type OfficeActionSprintEvent = {
  type:
    | 'sprint_start'
    | 'sprint_done'
    | 'sprint_stopped'
    | 'sprint_error'
    | 'hello'
    | 'plan_fallback'
    | 'budget_exceeded'
    | 'sprint_paused'
    | 'sprint_resumed'
    | 'artifact_deleted'
    | 'generate_progress'
    | 'instruction_added'
  [key: string]: unknown
}

export type OfficeActionHello = {
  type: 'hello'
  llm: boolean
  running: boolean
  tasks?: unknown[]
  artifacts?: OfficeArtifactPayload[]
  sprint?: SprintMetaPayload
  events?: unknown[]
  usage?: unknown
}

export type OfficeActionMessage =
  | OfficeActionDeskVisit
  | OfficeActionDeskVisitTour
  | OfficeActionSetState
  | OfficeActionEmote
  | OfficeActionTaskUpdate
  | OfficeActionArtifactNew
  | OfficeActionArtifactUpdate
  | OfficeActionStage
  | OfficeActionSprintUsage
  | OfficeActionSprintEvent
  | OfficeActionHello
