import { isOfficePaused, playAgentEmote, setAgentState } from '@/scene/officeSceneBridge'
import { submitVisitAction } from '@/services/officeActionDispatcher'
import { notifySprintOutcome } from '@/services/officeNotify'
import { playDoneSound } from '@/services/officeSound'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'
import {
  clearArtifacts,
  removeArtifact,
  setArtifacts,
  upsertArtifact,
  type OfficeArtifact,
} from '@/store/officeArtifactStore'
import { recordNotification, restoreNotifications } from '@/store/officeEventStore'
import { getOfficeAgents } from '@/store/officeStore'
import { showToast } from '@/store/officeUiStore'
import {
  getViewedSprint,
  refreshSprintHistory,
  reloadViewedSprint,
  setViewedSprint,
} from '@/store/officeViewStore'
import {
  applySprintMeta,
  applyTaskSnapshot,
  getSprintMetaSnapshot,
  getTasksSnapshot,
  pushActivity,
  resetSprintMeta,
  setSprintMetaCost,
  setSprintPaused,
  setSprintRunning,
  upsertTask,
  type SprintTask,
} from '@/store/officeTaskStore'
import type {
  OfficeActionMessage,
  OfficeArtifactPayload,
  SprintMetaPayload,
} from '@/types/officeAction'

type EmoteCapable = Extract<
  OfficeActionMessage,
  { rosterNo?: number; agentId?: string }
>

function resolveAgentId(message: EmoteCapable): string | undefined {
  const agents = getOfficeAgents()
  const agent = message.agentId
    ? agents.find((a) => a.id === message.agentId)
    : message.rosterNo != null
      ? agents[message.rosterNo - 1]
      : undefined
  return agent?.id
}

/** 每个智能体进行中的任务集合：并行开发时同一成员可能同时背多个任务，状态牌需合并展示 */
const activeTasksByAgent = new Map<string, Set<string>>()

function mergeTaskLabel(agentId: string, task?: string): string | undefined {
  if (!task) return task
  const set = activeTasksByAgent.get(agentId) ?? new Set<string>()
  set.add(task)
  activeTasksByAgent.set(agentId, set)
  // 最多展示最近 2 个任务，避免状态牌超长
  return [...set].slice(-2).join(' + ')
}

function dropTaskLabel(agentId: string, title?: string) {
  const set = activeTasksByAgent.get(agentId)
  if (!set || !title) return
  for (const item of [...set]) {
    if (item.includes(title)) set.delete(item)
  }
  if (set.size === 0) activeTasksByAgent.delete(agentId)
}

/** 把持久化的后端事件翻译回通知文案（用于刷新后恢复通知中心与时间线） */
function eventToText(event: Record<string, unknown>): string {
  switch (event.type) {
    case 'sprint_start':
      return `Sprint 开始：${String(event.requirement ?? '')}`
    case 'sprint_done':
      return 'Sprint 完成，团队进入空闲'
    case 'sprint_stopped':
      return '任务已手动结束'
    case 'sprint_error':
      return `Sprint 异常：${String(event.message ?? '未知错误')}`
    case 'budget_exceeded':
      return `⚠ ${String(event.message ?? '已达预算上限')}`
    case 'sprint_paused':
      return '已暂停：模型调用全部挂起'
    case 'sprint_resumed':
      return '已恢复：团队继续工作'
    case 'instruction_added':
      return `📌 老板补充了指示${event.text ? `：${String(event.text)}` : ''}`
    case 'stage':
      return `进入阶段：${String(event.stage ?? '')}`
    case 'plan_fallback':
      return `⚠ 任务拆解未按预期输出，已使用兜底拆解（${String(event.reason ?? '未知原因')}）`
    case 'desk_visit': {
      const visitorName = AGENT_ROSTER[Number(event.visitor) - 1]?.name ?? `#${event.visitor}`
      const hostName = AGENT_ROSTER[Number(event.host) - 1]?.name ?? `#${event.host}`
      return `${visitorName} → ${hostName}：${String(event.message ?? '')}`
    }
    case 'task_update': {
      const task = (event.task ?? {}) as { title?: string; id?: string; status?: string }
      return `「${task.title ?? task.id ?? ''}」${task.status ?? ''}`
    }
    case 'artifact_new': {
      const artifact = (event.artifact ?? {}) as { title?: string }
      return `新产物：《${artifact.title ?? ''}》`
    }
    case 'artifact_update': {
      const artifact = (event.artifact ?? {}) as { title?: string }
      return `产物已更新：《${artifact.title ?? ''}》`
    }
    default:
      return ''
  }
}

/** 把后端事件翻译成办公室动作（场景未就绪时由 bridge 排队兜底） */
export function dispatchOfficeAction(message: OfficeActionMessage) {
  console.info('[OfficeWS] received', message)

  // 全局暂停锁：丢弃一切会改变小人状态/位置的动作指令（数据类事件照常处理）
  const STATE_CHANGING = new Set(['desk_visit', 'desk_visit_tour', 'set_state', 'emote'])
  if (isOfficePaused() && STATE_CHANGING.has(message.type)) {
    console.info('[OfficeWS] 已暂停，忽略动作', message.type)
    return
  }

  switch (message.type) {
    case 'desk_visit': {
      submitVisitAction({
        type: 'desk_visit',
        visitor: message.visitor,
        host: message.host,
        message: message.message,
      })
      const visitorName =
        AGENT_ROSTER[message.visitor - 1]?.name ?? `#${message.visitor}`
      const hostName = AGENT_ROSTER[message.host - 1]?.name ?? `#${message.host}`
      const visitText = `${visitorName} → ${hostName}：${message.message}`
      pushActivity(visitText)
      recordNotification(visitText, 'desk_visit')
      return
    }
    case 'desk_visit_tour': {
      submitVisitAction({
        type: 'desk_visit_tour',
        visitor: message.visitor,
        hosts: message.hosts,
      })
      const tourName =
        AGENT_ROSTER[message.visitor - 1]?.name ?? `#${message.visitor}`
      recordNotification(`${tourName} 开始多站巡访`, 'desk_visit_tour')
      return
    }
    case 'set_state': {
      const agentId = resolveAgentId(message)
      if (!agentId) {
        console.warn('[OfficeWS] set_state: agent not found', message)
        return
      }
      if (message.state === 'working' || message.state === 'thinking') {
        setAgentState(agentId, message.state, mergeTaskLabel(agentId, message.task))
      } else {
        activeTasksByAgent.delete(agentId)
        setAgentState(agentId, message.state, message.task)
      }
      return
    }
    case 'emote': {
      const agentId = resolveAgentId(message)
      if (!agentId) {
        console.warn('[OfficeWS] emote: agent not found', message)
        return
      }
      playAgentEmote(agentId, message.animation, message.label)
      return
    }
    case 'task_update': {
      const task = message.task as SprintTask
      const previous = getTasksSnapshot().find((item) => item.id === task.id)
      // 正在浏览历史 Sprint 时，实时任务不写入看板（避免与历史数据混杂），仅记录动态
      if (!getViewedSprint()) upsertTask(task)
      // 任务离开开发态时，从执行者的状态牌集合中移除
      const assigneeRoster = (task as { assignee_roster?: number }).assignee_roster
      if (
        assigneeRoster != null &&
        task.status != null &&
        !['待开发', '打回'].includes(task.status)
      ) {
        const assigneeId = getOfficeAgents()[assigneeRoster - 1]?.id
        if (assigneeId) dropTaskLabel(assigneeId, task.title)
      }
      if (!previous || previous.status !== task.status) {
        const statusText = `「${task.title ?? task.id}」${task.status}`
        pushActivity(statusText)
        recordNotification(statusText, 'task_update')
      }
      return
    }
    case 'sprint_start': {
      // 新 Sprint 开始：切回实时视角并刷新历史列表
      if (getViewedSprint()) void setViewedSprint('')
      void refreshSprintHistory()
      setSprintRunning(true)
      applyTaskSnapshot([])
      clearArtifacts()
      resetSprintMeta()
      const requirement = (message as { requirement?: unknown }).requirement
      applySprintMeta({
        requirement: String(requirement ?? ''),
        status: 'running',
        stage: '准备开工',
        startedAt: Date.now() / 1000,
        finishedAt: 0,
      })
      const startText = `Sprint 开始：${String(requirement ?? '')}`
      pushActivity(startText)
      recordNotification(startText, 'sprint_start')
      return
    }
    case 'stage': {
      const stage = (message as { stage?: unknown }).stage
      const stageStarted = (message as { stage_started?: unknown }).stage_started
      applySprintMeta({
        stage: String(stage ?? ''),
        stageStarted: Number(stageStarted ?? Date.now() / 1000),
      })
      return
    }
    case 'sprint_usage': {
      const cost = (message as { cost?: unknown }).cost
      setSprintMetaCost(Number(cost ?? 0))
      return
    }
    case 'artifact_new': {
      const artifact = (message as { artifact?: OfficeArtifact }).artifact
      if (!artifact) return
      if (!getViewedSprint()) upsertArtifact(artifact)
      recordNotification(`新产物：《${artifact.title}》`, 'artifact_new')
      return
    }
    case 'artifact_update': {
      const artifact = (message as { artifact?: OfficeArtifact }).artifact
      if (!artifact) return
      if (!getViewedSprint()) upsertArtifact(artifact)
      recordNotification(`产物已更新：《${artifact.title}》`, 'artifact_new')
      return
    }
    case 'sprint_done': {
      void refreshSprintHistory()
      setSprintRunning(false)
      applySprintMeta({ status: 'completed', stage: '已完成' })
      playDoneSound()
      pushActivity('Sprint 完成，团队进入空闲')
      recordNotification('Sprint 完成，团队进入空闲', 'sprint_done')
      showToast('🎉 Sprint 完成！最终交付物已生成，请到产物中心查看', {
        kind: 'success',
      })
      notifySprintOutcome('done', getSprintMetaSnapshot().requirement)
      return
    }
    case 'sprint_stopped': {
      setSprintRunning(false)
      applySprintMeta({ status: 'stopped', stage: '已手动结束' })
      for (const agent of getOfficeAgents()) {
        setAgentState(agent.id, 'idle', '等待新任务')
      }
      activeTasksByAgent.clear()
      pushActivity('任务已手动结束')
      recordNotification('任务已手动结束', 'sprint_done')
      showToast('任务已结束，团队进入空闲')
      return
    }
    case 'sprint_error': {
      setSprintRunning(false)
      const errorMessage = (message as { message?: unknown }).message
      const errorText = `Sprint 异常：${String(errorMessage ?? '未知错误')}`
      applySprintMeta({ status: 'failed' })
      pushActivity(errorText)
      recordNotification(errorText, 'sprint_error')
      showToast(errorText)
      notifySprintOutcome('error', getSprintMetaSnapshot().requirement)
      return
    }
    case 'plan_fallback': {
      const reason = (message as { reason?: unknown }).reason
      const text = `⚠ 任务拆解未按预期输出，已使用兜底拆解（${String(reason ?? '未知原因')}）`
      pushActivity(text)
      recordNotification(text, 'plan_fallback')
      showToast(text)
      return
    }
    case 'budget_exceeded': {
      const text = `⚠ ${String((message as { message?: unknown }).message ?? '已达预算上限')}`
      setSprintRunning(false)
      applySprintMeta({ status: 'completed', stage: '已达预算上限' })
      pushActivity(text)
      recordNotification(text, 'budget_exceeded')
      showToast(text)
      notifySprintOutcome('budget', getSprintMetaSnapshot().requirement)
      return
    }
    case 'sprint_paused': {
      setSprintPaused(true)
      const text = '已暂停：后端模型调用全部挂起，不再产生费用'
      pushActivity(text)
      recordNotification(text, 'sprint_paused')
      showToast(text)
      return
    }
    case 'sprint_resumed': {
      setSprintPaused(false)
      pushActivity('已恢复：团队继续工作')
      recordNotification('已恢复：团队继续工作', 'sprint_resumed')
      showToast('已恢复工作')
      return
    }
    case 'instruction_added': {
      const text = String((message as { text?: unknown }).text ?? '')
      const full = `📌 老板补充了指示${text ? `：${text}` : ''}`
      pushActivity(full)
      recordNotification(full, 'instruction_added')
      showToast(full)
      return
    }
    case 'artifact_deleted': {
      const id = (message as { artifact_id?: unknown }).artifact_id
      if (typeof id === 'string' && !getViewedSprint()) removeArtifact(id)
      return
    }
    case 'generate_progress': {
      const stage = String((message as { stage?: unknown }).stage ?? '生成中')
      pushActivity(`直接生成：${stage}`)
      showToast(`直接生成：${stage}…`)
      return
    }
    case 'hello': {
      // 断线/刷新重连：用后端完整快照恢复运行状态、任务、产物、Sprint 阶段与历史事件
      const snapshot = message as {
        tasks?: unknown
        artifacts?: OfficeArtifactPayload[]
        sprint?: SprintMetaPayload & { budget?: number }
        running?: boolean
        paused?: boolean
        events?: Array<Record<string, unknown>>
      }
      if (Array.isArray(snapshot.tasks) && !getViewedSprint()) {
        applyTaskSnapshot(snapshot.tasks as SprintTask[])
      }
      if (Array.isArray(snapshot.artifacts) && !getViewedSprint()) {
        setArtifacts(snapshot.artifacts as OfficeArtifact[])
      }
      // 正在浏览历史 Sprint：重连后重新拉取历史数据，避免被实时快照覆盖
      if (getViewedSprint()) void reloadViewedSprint()
      if (snapshot.sprint) {
        applySprintMeta({
          id: snapshot.sprint.id ?? '',
          requirement: snapshot.sprint.requirement ?? '',
          stage: snapshot.sprint.stage ?? '',
          stageStarted: snapshot.sprint.stage_started ?? 0,
          startedAt: snapshot.sprint.started_at ?? 0,
          finishedAt: snapshot.sprint.finished_at ?? 0,
          status: snapshot.sprint.status ?? 'idle',
          fast: Boolean(snapshot.sprint.fast),
          budget: Number(snapshot.sprint.budget ?? 0),
        })
      }
      setSprintRunning(Boolean(snapshot.running))
      setSprintPaused(Boolean(snapshot.paused))
      // 恢复通知中心/时间线：把持久化事件翻译回可读通知（不产生未读角标）
      if (Array.isArray(snapshot.events) && snapshot.events.length > 0) {
        const restored: { text: string; kind: string; time: string }[] = []
        for (const event of snapshot.events) {
          const text = eventToText(event)
          if (!text) continue
          const ts = Number(event.ts ?? 0)
          const d = ts ? new Date(ts * 1000) : new Date()
          const hh = String(d.getHours()).padStart(2, '0')
          const mm = String(d.getMinutes()).padStart(2, '0')
          const ss = String(d.getSeconds()).padStart(2, '0')
          restored.push({ text, kind: String(event.type ?? ''), time: `${hh}:${mm}:${ss}` })
        }
        restoreNotifications(restored.reverse())
      }
      pushActivity('已连接敏捷开发后端')
      return
    }
    default: {
      const _exhaustive: never = message
      console.warn('[OfficeWS] unknown message type', _exhaustive)
    }
  }
}
