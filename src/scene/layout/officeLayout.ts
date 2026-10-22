import type { Agent, AgentState, Desk } from '@/types/agent'

export const SCENE_WIDTH = 960
export const SCENE_HEIGHT = 640

export const COLORS = {
  floor: 0xffffff,
  wall: 0xe8e6e1,
  desk: 0xffffff,
  deskShadow: 0x00000014,
  monitor: 0x2a2a2a,
  chair: 0xd4d2cc,
  agentBody: 0x1a1a1a,
} as const

/** 2×3 工位区 */
const DESK_COLS = 2
const DESK_ROWS = 3
const DESK_COL_GAP = 150
const DESK_ROW_GAP = 140
const DESK_BLOCK_WIDTH = (DESK_COLS - 1) * DESK_COL_GAP
const DESK_BLOCK_HEIGHT = (DESK_ROWS - 1) * DESK_ROW_GAP
/** 工位阵列在场景内水平/垂直居中 */
const DESK_ORIGIN_X = (SCENE_WIDTH - DESK_BLOCK_WIDTH) / 2
const DESK_ORIGIN_Y = (SCENE_HEIGHT - DESK_BLOCK_HEIGHT) / 2
export const SEAT_OFFSET_Y = 45

function buildDesks(): Desk[] {
  const desks: Desk[] = []
  let n = 0
  for (let row = 0; row < DESK_ROWS; row++) {
    for (let col = 0; col < DESK_COLS; col++) {
      const x = DESK_ORIGIN_X + col * DESK_COL_GAP
      const y = DESK_ORIGIN_Y + row * DESK_ROW_GAP
      desks.push({
        id: `desk-${n}`,
        x,
        y,
        seatX: x,
        seatY: y + SEAT_OFFSET_Y,
      })
      n++
    }
  }
  return desks
}

export const DESKS: Desk[] = buildDesks()

export type AgentRosterEntry = {
  id: string
  name: string
  role: string
  skills: string[]
  color: number
  task: string
}

/** 6 位敏捷团队员工（名册序号 1–6，与后端 roles.py 一一对应） */
export const AGENT_ROSTER: AgentRosterEntry[] = [
  {
    id: 'marvis',
    name: '王明',
    role: '产品经理',
    skills: ['需求拆解', '用户故事', '优先级排序'],
    color: 0xe85d4a,
    task: '产品：整理需求池',
  },
  {
    id: 'code-agent',
    name: '李研',
    role: '后端工程师',
    skills: ['接口设计', '数据库', '服务编排'],
    color: 0x4a90d9,
    task: '后端：维护接口服务',
  },
  {
    id: 'file-agent',
    name: '周理',
    role: '前端工程师',
    skills: ['页面还原', '交互动效', '组件沉淀'],
    color: 0x9b6dd7,
    task: '前端：打磨页面细节',
  },
  {
    id: 'app-agent',
    name: '陈书',
    role: '测试工程师',
    skills: ['用例设计', '回归测试', '缺陷跟踪'],
    color: 0xf5c542,
    task: '测试：维护用例库',
  },
  {
    id: 'review-agent',
    name: '刘市',
    role: '技术主管',
    skills: ['代码评审', '架构把关', '技术选型'],
    color: 0xf97316,
    task: '主管：巡查代码质量',
  },
  {
    id: 'data-agent',
    name: '赵审',
    role: '敏捷教练',
    skills: ['流程保障', '站会组织', '阻塞排除'],
    color: 0x4ecdc4,
    task: '教练：保障流程运转',
  },
]

const BOOT_STATES: AgentState[] = [
  'working',
  'working',
  'working',
  'working',
  'working',
  'working',
]

function buildInitialAgents(): Agent[] {
  return AGENT_ROSTER.map((entry, i) => {
    const desk = DESKS[i]
    const state = BOOT_STATES[i] ?? 'idle'
    return {
      id: entry.id,
      name: entry.name,
      color: entry.color,
      x: desk.seatX,
      y: desk.seatY,
      state,
      currentTask: state === 'idle' ? undefined : entry.task,
      assignedDeskId: desk.id,
      facing: i % 2 === 0 ? 1 : -1,
      viewFacing:
        state === 'working' || state === 'thinking' ? ('back' as const) : ('front' as const),
    }
  })
}

export const INITIAL_AGENTS: Agent[] = buildInitialAgents()

/** 交接流程中的状态标签（头顶 / 侧栏） */
export const HANDOFF_STATUS = {
  delivering: '交接递送中…',
  handingOff: '正在交接…',
  receiving: '接收交接中…',
  wrappingUp: '交接收尾中…',
  planning: '规划交接中…',
} as const

/** 离座拜访时交给对方的话术 */
export const HANDOFF_VISIT_MESSAGES: ((hostName: string) => string)[] = [
  (n) => `${n}，这件事交给你了。`,
  (n) => `${n}，轮到你了，说明在工单里。`,
  (n) => `${n}，接力给你，上下文在线程里。`,
  (n) => `${n}，你队列里有最新的交接包。`,
  (n) => `${n}，工单已转给你，我这边解除了阻塞。`,
  (n) => `${n}，能从这里接手吗？`,
  (n) => `${n}，我这边交接完成，交给你了。`,
  (n) => `${n}，收到后请确认一下。`,
]

export function pickHandoffVisitMessage(
  hostName: string,
  hostRosterNo: number,
): string {
  const i = Math.abs(hostRosterNo - 1) % HANDOFF_VISIT_MESSAGES.length
  return HANDOFF_VISIT_MESSAGES[i]!(hostName)
}
