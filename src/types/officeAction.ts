import type { AgentState } from '@/types/agent'

/** 多回对对话剧本中的一句：speaker 为访客或被访者 */
export type OfficeDialogLine = {
  speaker: 'visitor' | 'host'
  text: string
}

export type OfficeActionDeskVisit = {
  type: 'desk_visit'
  visitor: number
  host: number
  message: string
  /** 多回合对话剧本（可选）：前端按节拍分播，缺省时按单句 message 展示 */
  lines?: OfficeDialogLine[]
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
  /** 非 Sprint 的状态更新（独立产物生成 / 单兵任务），Sprint 结束后仍可更新负责成员。 */
  source?: 'artifact_generation' | 'solo_task'
}

export type OfficeActionEmote = {
  type: 'emote'
  /** Spine 动画名，如 emotes/idea */
  animation: string
  label?: string
  rosterNo?: number
  agentId?: string
}

/** 成员思考/决策依据事件：让「为什么这么做」对用户可见 */
export type OfficeActionThought = {
  type: 'thought'
  rosterNo: number
  thought: string
  /** 所处阶段，如「需求拆解」「并行开发」 */
  phase?: string
  /** 关联任务 ID（可选） */
  taskId?: string
}

/** 交接事件：offer/receive 时推送，前端仅将阶段级交接（无 task_id）记入通知中心 */
export type OfficeActionHandoff = {
  type: 'handoff'
  handoff: {
    id: number
    sprint_id: string
    task_id: string
    from_roster: number
    to_roster: number
    claims: string[]
    open_questions?: string[]
    acceptance_criteria?: string[]
    status: string
    reason?: string
    version?: number
  }
  sprint_id?: string
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
    | 'sprint_end'
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

/** 单兵任务（单飞）进度事件：完成/失败时推送，更新聊天任务卡片 */
export type OfficeActionSoloTaskUpdate = {
  type: 'solo_task_update'
  roster: number
  task_id: string
  title: string
  status: 'done' | 'failed'
  artifact_id?: string | null
  summary?: string
}

export type OfficeActionMessage =
  | OfficeActionDeskVisit
  | OfficeActionDeskVisitTour
  | OfficeActionSetState
  | OfficeActionEmote
  | OfficeActionThought
  | OfficeActionHandoff
  | OfficeActionTaskUpdate
  | OfficeActionArtifactNew
  | OfficeActionArtifactUpdate
  | OfficeActionStage
  | OfficeActionSprintUsage
  | OfficeActionSprintEvent
  | OfficeActionHello
  | OfficeActionSoloTaskUpdate
