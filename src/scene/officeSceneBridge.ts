import type { OfficeScene } from '@/scene/OfficeScene'
import { submitVisitAction } from '@/services/officeActionDispatcher'
import type { VisitActionMessage } from '@/services/officeActionDispatcher'
import { getOfficeAgents, setOfficeAgents } from '@/store/officeStore'
import type { AgentState } from '@/types/agent'

let scene: OfficeScene | null = null
let officePaused = false
/** 夜晚模式（暗色主题联动）：场景重建后依然保持，与暂停锁同模式 */
let nightMode = false

/** 切换办公室夜晚灯光：场景未就绪时只记旗标，绑定后自动应用 */
export function setNightMode(on: boolean) {
  nightMode = on
  scene?.setNightMode(on)
}

/** 全局暂停状态：场景重建后依然保持，WS 动作指令据此拦截 */
export function setOfficePaused(paused: boolean) {
  officePaused = paused
  scene?.setPaused(paused)
}

export function isOfficePaused() {
  return officePaused
}

type PendingDeskVisit = {
  kind: 'desk_visit'
  visitorRosterNo: number
  hostRosterNo: number
  message: string
}

type PendingDeskVisitTour = {
  kind: 'desk_visit_tour'
  visitorRosterNo: number
  hostRosterNos: number[]
  messageFn?: (hostRosterNo: number, hostName: string) => string
}

type PendingSetState = {
  kind: 'set_state'
  agentId: string
  state: AgentState
  task?: string
}

type PendingPlayEmote = {
  kind: 'play_emote'
  agentId: string
  animation: string
  task?: string
}

type PendingRename = {
  kind: 'rename'
  agentId: string
  name: string
}

type PendingSetAvatar = {
  kind: 'set_avatar'
  agentId: string
  avatar: string
}

type PendingSetSkin = {
  kind: 'set_skin'
  agentId: string
  skin: string
}

type PendingAction = PendingDeskVisit | PendingDeskVisitTour | PendingSetState | PendingPlayEmote | PendingRename | PendingSetAvatar | PendingSetSkin

const pendingActions: PendingAction[] = []

function flushPendingActions() {
  if (!scene || pendingActions.length === 0) return

  const queue = pendingActions.splice(0)
  console.info('[OfficeHTTP] scene ready, flushing', queue.length, 'pending action(s)')
  for (const action of queue) {
    if (action.kind === 'desk_visit') {
      submitVisitAction({
        type: 'desk_visit',
        visitor: action.visitorRosterNo,
        host: action.hostRosterNo,
        message: action.message,
      })
    } else if (action.kind === 'desk_visit_tour') {
      submitVisitAction(
        {
          type: 'desk_visit_tour',
          visitor: action.visitorRosterNo,
          hosts: action.hostRosterNos,
        },
        { messageFn: action.messageFn },
      )
    } else if (action.kind === 'play_emote') {
      scene.playAgentAnimation(action.agentId, action.animation, action.task)
    } else if (action.kind === 'rename') {
      scene.setAgentName(action.agentId, action.name)
    } else if (action.kind === 'set_avatar') {
      scene.setAgentAvatar(action.agentId, action.avatar)
    } else if (action.kind === 'set_skin') {
      scene.setAgentSkin(action.agentId, action.skin)
    } else {
      scene.setAgentState(action.agentId, action.state, action.task)
    }
  }
}

export function bindOfficeScene(instance: OfficeScene | null) {
  scene = instance
  if (instance) {
    flushPendingActions()
    instance.setPaused(officePaused)
    instance.setNightMode(nightMode)
  }
}

/** 名册序号从 1 开始，例如 1 号去找 5 号 */
export function requestDeskVisit(
  visitorRosterNo: number,
  hostRosterNo: number,
  message: string,
) {
  if (!scene) {
    pendingActions.push({
      kind: 'desk_visit',
      visitorRosterNo,
      hostRosterNo,
      message,
    })
    console.info('[OfficeHTTP] scene not ready, pending desk_visit', {
      visitor: visitorRosterNo,
      host: hostRosterNo,
    })
    return
  }
  scene.requestDeskVisit(visitorRosterNo, hostRosterNo, message)
}

/** 1 号依次拜访 2、3、4… 号，全部说完后回座 */
export function requestDeskVisitTour(
  visitorRosterNo: number,
  hostRosterNos: number[],
  messageFn?: (hostRosterNo: number, hostName: string) => string,
) {
  if (!scene) {
    pendingActions.push({
      kind: 'desk_visit_tour',
      visitorRosterNo,
      hostRosterNos,
      messageFn,
    })
    console.info('[OfficeHTTP] scene not ready, pending desk_visit_tour', {
      visitor: visitorRosterNo,
      hosts: hostRosterNos,
    })
    return
  }
  scene.requestDeskVisitTour(visitorRosterNo, hostRosterNos, messageFn)
}

/** 自定义成员姓名：场景就绪直接改名牌，否则先改 officeStore 并排队等场景绑定 */
export function renameAgent(agentId: string, name: string) {
  if (!name) return
  if (!scene) {
    setOfficeAgents(
      getOfficeAgents().map((a) => (a.id === agentId ? { ...a, name } : a)),
    )
    // 队列里只保留该成员最新一次改名
    for (let i = pendingActions.length - 1; i >= 0; i--) {
      const action = pendingActions[i]!
      if (action.kind === 'rename' && action.agentId === agentId) {
        pendingActions.splice(i, 1)
      }
    }
    pendingActions.push({ kind: 'rename', agentId, name })
    return
  }
  scene.setAgentName(agentId, name)
}

/** 自定义成员头像 emoji：场景就绪直接改名牌，否则排队等场景绑定（无需写入 officeStore） */
export function setAgentAvatar(agentId: string, avatar: string) {
  if (!scene) {
    // 队列里只保留该成员最新一次头像设置
    for (let i = pendingActions.length - 1; i >= 0; i--) {
      const action = pendingActions[i]!
      if (action.kind === 'set_avatar' && action.agentId === agentId) {
        pendingActions.splice(i, 1)
      }
    }
    pendingActions.push({ kind: 'set_avatar', agentId, avatar })
    return
  }
  scene.setAgentAvatar(agentId, avatar)
}

/** 自定义成员人物形象皮肤：场景就绪直接换肤，否则排队等场景绑定 */
export function setAgentSkin(agentId: string, skin: string) {
  if (!scene) {
    // 队列里只保留该成员最新一次皮肤设置
    for (let i = pendingActions.length - 1; i >= 0; i--) {
      const action = pendingActions[i]!
      if (action.kind === 'set_skin' && action.agentId === agentId) {
        pendingActions.splice(i, 1)
      }
    }
    pendingActions.push({ kind: 'set_skin', agentId, skin })
    return
  }
  scene.setAgentSkin(agentId, skin)
}

/** 导出当前办公室画布为 PNG dataURL；场景未就绪时返回 null */
export function captureOfficeScreenshot(): string | null {
  return scene?.captureScreenshot() ?? null
}

export function setAgentState(agentId: string, state: AgentState, task?: string) {
  if (!scene) {
    setOfficeAgents(
      getOfficeAgents().map((agent) =>
        agent.id === agentId ? { ...agent, state, currentTask: task } : agent,
      ),
    )
    // 同一成员只保留最新状态，避免场景稍后挂载时重放过期状态。
    for (let i = pendingActions.length - 1; i >= 0; i--) {
      const action = pendingActions[i]!
      if (action.kind === 'set_state' && action.agentId === agentId) {
        pendingActions.splice(i, 1)
      }
    }
    pendingActions.push({
      kind: 'set_state',
      agentId,
      state,
      task,
    })
    console.info('[OfficeHTTP] scene not ready, pending set_state', {
      agentId,
      state,
    })
    return
  }
  scene.setAgentState(agentId, state, task)
}

/** 播放表情动画，例如 emotes/idea */
export function playAgentEmote(agentId: string, animation: string, task?: string) {
  if (!scene) {
    pendingActions.push({
      kind: 'play_emote',
      agentId,
      animation,
      task,
    })
    console.info('[OfficeHTTP] scene not ready, pending play_emote', {
      agentId,
      animation,
    })
    return
  }
  scene.playAgentAnimation(agentId, animation, task)
}

export type { VisitActionMessage }
