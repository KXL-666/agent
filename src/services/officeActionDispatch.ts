import { isOfficePaused, playAgentEmote, setAgentState, showAgentThought } from '@/scene/officeSceneBridge'
import { submitVisitAction } from '@/services/officeActionDispatcher'
import { notifySprintOutcome } from '@/services/officeNotify'
import { playDoneSound, playTaskSound } from '@/services/officeSound'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'
import {
  clearArtifacts,
  getArtifactsSnapshot,
  removeArtifact,
  setArtifacts,
  upsertArtifact,
  type OfficeArtifact,
} from '@/store/officeArtifactStore'
import { clearNotifications, recordNotification, restoreNotifications } from '@/store/officeEventStore'
import { appendAssistantMessage, appendHintMessage, appendHintToConversation, findConversationBySprint, getActiveConversationId, markConversationSprint, updateTaskMessage } from '@/store/officeChatStore'
import { getOfficeAgents } from '@/store/officeStore'
import { addThought, clearThoughts } from '@/store/officeThoughtStore'
import { pulseArtifacts, showToast } from '@/store/officeUiStore'
import {
  getViewedSprint,
  refreshSprintHistory,
  reloadViewedSprint,
  setViewedSprint,
} from '@/store/officeViewStore'
import {
  applySprintMeta,
  applyTaskSnapshot,
  clearActivities,
  getSprintMetaSnapshot,
  getTasksSnapshot,
  isSprintRunning,
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

type EmoteCapable = Exclude<
  Extract<OfficeActionMessage, { rosterNo?: number }>,
  { type: 'thought' }
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
let sprintTerminal = false

function idleAllAgents() {
  activeTasksByAgent.clear()
  for (const agent of getOfficeAgents()) setAgentState(agent.id, 'idle', '等待新任务')
}

function mergeTaskLabel(agentId: string, task?: string): string | undefined {
  if (!task) return task
  const set = activeTasksByAgent.get(agentId) ?? new Set<string>()
  set.add(task)
  activeTasksByAgent.set(agentId, set)
  // 最多展示最近 2 个任务标题，更多时追加总数，避免状态牌超长
  const labels = [...set].slice(-2).join(' + ')
  return set.size > 2 ? `${labels}（共 ${set.size} 项）` : labels
}

function dropTaskLabel(agentId: string, title?: string) {
  const set = activeTasksByAgent.get(agentId)
  if (!set || !title) return
  for (const item of [...set]) {
    if (item.includes(title)) set.delete(item)
  }
  if (set.size === 0) activeTasksByAgent.delete(agentId)
}

function artifactDetail(artifact: OfficeArtifact): string {
  const kindLabels: Record<string, string> = {
    prd: '需求拆解', dev: '开发记录', test: '测试记录', review: '评审记录',
    summary: '复盘记录', final: '最终产物', custom: '最终产物',
  }
  const sourceLabels: Record<string, string> = {
    demo: '脚本演示', model: '真实模型', fallback: '兜底模板', local: '本地生成', unknown: '未知',
  }
  const qualityLabels: Record<string, string> = {
    passed: '通过', failed: '未通过', unchecked: '未检查', unknown: '未知',
  }
  const lines = [
    `类型：${kindLabels[artifact.kind] ?? artifact.kind}`,
    `来源：${sourceLabels[artifact.source ?? 'unknown'] ?? artifact.source ?? '未知'}`,
    `文件：${artifact.file_name || '无独立文件，仅文字内容'}`,
    `验收：${qualityLabels[artifact.quality_status ?? 'unchecked'] ?? artifact.quality_status ?? '未检查'}`,
  ]
  if (artifact.degraded_reason) lines.push(`问题说明：${artifact.degraded_reason}`)
  if (artifact.content) lines.push('', '内容：', artifact.content)
  return lines.join('\n')
}

function finalQualityDetail(): string {
  const finals = getArtifactsSnapshot().filter((artifact) => artifact.kind === 'final' || artifact.kind === 'custom')
  if (!finals.length) return '本轮没有可用的最终产物记录。'
  return finals.map((artifact) => [
    `产物：${artifact.title}`,
    `验收状态：${artifact.quality_status === 'passed' ? '通过' : artifact.quality_status === 'failed' ? '未通过' : '未检查'}`,
    artifact.file_name ? `文件：${artifact.file_name}` : '文件：未生成，当前为文字摘要',
    artifact.degraded_reason ? `问题原因：${artifact.degraded_reason}` : '',
  ].filter(Boolean).join('\n')).join('\n\n')
}

/** 手动结束后的界面清理（幂等）：在发起对话里留文字记录，再清掉本轮界面残留 */
function finalizeManualStop() {
  const metaBeforeReset = getSprintMetaSnapshot()
  if (!metaBeforeReset.id) return
  const endedTasks = getTasksSnapshot()
  const doneCount = endedTasks.filter((task) => task.status === '通过').length
  const artifactCount = getArtifactsSnapshot().length
  const recordText = `本轮任务已手动结束：通过验收 ${doneCount}/${endedTasks.length} 项，产出 ${artifactCount} 件产物，累计花费 ¥${metaBeforeReset.cost.toFixed(3)}。历史可在「任务」视图中回看。`
  const ownerConv = findConversationBySprint(0, metaBeforeReset.id)
  if (ownerConv) {
    appendHintToConversation(0, ownerConv, recordText)
  } else {
    appendHintMessage(0, recordText)
  }
  applyTaskSnapshot([])
  clearActivities()
  resetSprintMeta()
}

function restoredEventDetail(event: Record<string, unknown>): { title?: string; detail?: string } {
  const artifact = event.artifact as OfficeArtifact | undefined
  if (artifact && ['prd', 'dev', 'test', 'review', 'summary', 'final', 'custom'].includes(artifact.kind)) {
    return { title: artifact.title, detail: artifactDetail(artifact) }
  }
  switch (event.type) {
    case 'sprint_start':
      return { title: '本轮工作内容', detail: `需求：${String(event.requirement ?? '未填写')}\n状态：已启动` }
    case 'sprint_done':
      return { title: event.accepted === false ? '验收失败详情' : '本轮交付结果', detail: finalQualityDetail() }
    case 'sprint_stopped':
      return { title: '结束时的交付状态', detail: finalQualityDetail() }
    case 'sprint_error':
      return { title: '错误详情', detail: String(event.message ?? '未知错误') }
    case 'task_update': {
      const task = (event.task ?? {}) as SprintTask
      return {
        title: '任务状态详情',
        detail: [
          `任务：${task.title ?? task.id ?? '未命名'}`,
          `状态：${task.status ?? '未知'}`,
          task.priority ? `优先级：${task.priority}` : '',
          task.assignee_roster ? `负责人：${AGENT_ROSTER[task.assignee_roster - 1]?.name ?? `成员 ${task.assignee_roster}`}` : '负责人：待分配',
          task.summary ? `说明：${task.summary}` : '',
          task.acceptance_criteria?.length ? `验收标准：\n${task.acceptance_criteria.map((criterion) => `• ${criterion}`).join('\n')}` : '',
        ].filter(Boolean).join('\n'),
      }
    }
    case 'instruction_added':
      return { title: '补充指示', detail: String(event.text ?? '未提供指示内容') }
    case 'thought': {
      const rosterNo = Number(event.rosterNo ?? 0)
      const name = AGENT_ROSTER[rosterNo - 1]?.name ?? `#${rosterNo}`
      const phase = event.phase ? String(event.phase) : ''
      return {
        title: `${name} 的决策依据${phase ? ` · ${phase}` : ''}`,
        detail: String(event.thought ?? ''),
      }
    }
    case 'handoff': {
      const handoff = (event.handoff ?? {}) as {
        from_roster?: number
        to_roster?: number
        claims?: string[]
        open_questions?: string[]
        status?: string
        reason?: string
      }
      const fromName = AGENT_ROSTER[Number(handoff.from_roster) - 1]?.name ?? `#${handoff.from_roster}`
      const toName = AGENT_ROSTER[Number(handoff.to_roster) - 1]?.name ?? `#${handoff.to_roster}`
      return {
        title: `交接 · ${fromName} → ${toName}`,
        detail: [
          `状态：${handoff.status ?? '未知'}`,
          handoff.claims?.length ? `声明：${handoff.claims.join('；')}` : '',
          handoff.open_questions?.length ? `待确认：${handoff.open_questions.join('；')}` : '',
          handoff.reason ? `说明：${handoff.reason}` : '',
        ].filter(Boolean).join('\n'),
      }
    }
    case 'plan_fallback':
      return { title: '拆解兜底原因', detail: String(event.reason ?? '未提供具体原因') }
    case 'budget_exceeded':
      return { title: '预算限制详情', detail: String(event.message ?? '已达到预算上限') }
    case 'sprint_paused':
      return { title: '暂停说明', detail: `当前需求：${getSprintMetaSnapshot().requirement || '未记录'}\n模型调用已挂起。` }
    case 'sprint_resumed':
      return { title: '恢复工作', detail: `当前需求：${getSprintMetaSnapshot().requirement || '未记录'}\n团队已继续处理本轮任务。` }
    default:
      return {}
  }
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
    case 'thought': {
      const rosterNo = Number(event.rosterNo ?? 0)
      const name = AGENT_ROSTER[rosterNo - 1]?.name ?? `#${rosterNo}`
      const text = String(event.thought ?? '')
      const phase = event.phase ? String(event.phase) : ''
      return text
        ? `💭 ${name}${phase ? `（${phase}）` : ''}：${text.length > 60 ? `${text.slice(0, 60)}…` : text}`
        : ''
    }
    case 'handoff': {
      const handoff = (event.handoff ?? {}) as { task_id?: string; from_roster?: number; to_roster?: number; claims?: string[] }
      if (handoff.task_id) return '' // 任务级交接不在时间线恢复，避免刷屏
      const fromName = AGENT_ROSTER[Number(handoff.from_roster) - 1]?.name ?? `#${handoff.from_roster}`
      const toName = AGENT_ROSTER[Number(handoff.to_roster) - 1]?.name ?? `#${handoff.to_roster}`
      return `🤝 ${fromName} → ${toName}：${(handoff.claims ?? []).join('；') || '交接'}`
    }
    case 'task_update': {
      const task = (event.task ?? {}) as { title?: string; id?: string; status?: string }
      return `「${task.title ?? task.id ?? ''}」${task.status ?? ''}`
    }
    case 'artifact_new': {
      const artifact = (event.artifact ?? {}) as OfficeArtifact
      const labels: Record<string, string> = { prd: '需求拆解', dev: '开发记录', test: '测试记录', review: '评审记录', summary: '复盘记录' }
      return labels[artifact.kind ?? '']
        ? `${labels[artifact.kind ?? '']}已生成：《${artifact.title ?? ''}》`
        : `最终产物已生成：《${artifact.title ?? ''}》`
    }
    case 'artifact_update': {
      const artifact = (event.artifact ?? {}) as OfficeArtifact
      return `${artifact.kind === 'review' ? '评审记录' : '产物'}已更新：《${artifact.title ?? ''}》`
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
      // Sprint 已结束：取消瞬间仍在飞行的 speak 线程会迟到补发拜访事件，直接丢弃
      if (sprintTerminal) return
      submitVisitAction({
        type: 'desk_visit',
        visitor: message.visitor,
        host: message.host,
        message: message.message,
        lines: message.lines,
      })
      const visitorName =
        AGENT_ROSTER[message.visitor - 1]?.name ?? `#${message.visitor}`
      const hostName = AGENT_ROSTER[message.host - 1]?.name ?? `#${message.host}`
      const visitText = `${visitorName} → ${hostName}：${message.message}`
      pushActivity(visitText)
      recordNotification(visitText, 'desk_visit')
      return
    }
    case 'thought': {
      if (sprintTerminal) return
      // 成员决策依据：入思考流 store + 场景头顶思考泡 + 通知中心可回溯全文
      const rosterNo = Number(message.rosterNo ?? 0)
      const text = String(message.thought ?? '').trim()
      if (!rosterNo || !text) return
      const phase = String(message.phase ?? '')
      const taskId = message.taskId ? String(message.taskId) : undefined
      addThought({ rosterNo, thought: text, phase, taskId })
      const agentId = getOfficeAgents()[rosterNo - 1]?.id
      if (agentId) showAgentThought(agentId, text)
      const name = AGENT_ROSTER[rosterNo - 1]?.name ?? `#${rosterNo}`
      recordNotification(
        `💭 ${name}${phase ? `（${phase}）` : ''}：${text.length > 60 ? `${text.slice(0, 60)}…` : text}`,
        'thought',
        { title: `${name} 的决策依据${phase ? ` · ${phase}` : ''}`, detail: text },
      )
      return
    }
    case 'handoff': {
      if (sprintTerminal) return
      // 交接叙事：仅阶段级交接（无 task_id）进通知中心，任务级交接由气泡与任务卡覆盖
      const handoff = (message as { handoff?: Record<string, unknown> }).handoff
      if (!handoff || handoff.task_id) return
      const fromName = AGENT_ROSTER[Number(handoff.from_roster) - 1]?.name ?? `#${handoff.from_roster}`
      const toName = AGENT_ROSTER[Number(handoff.to_roster) - 1]?.name ?? `#${handoff.to_roster}`
      const claims = Array.isArray(handoff.claims) ? handoff.claims.join('；') : ''
      const statusText = String(handoff.status ?? '')
      const statusLabel = statusText === 'accepted' ? '已接收' : statusText === 'ready' ? '待接收' : statusText
      recordNotification(
        `🤝 ${fromName} → ${toName}：${claims || '交接'}（${statusLabel}）`,
        'handoff',
        {
          title: `交接 · ${fromName} → ${toName}`,
          detail: [
            `状态：${statusLabel}`,
            claims ? `声明：${claims}` : '',
            Array.isArray(handoff.open_questions) && handoff.open_questions.length
              ? `待确认：${handoff.open_questions.join('；')}`
              : '',
            handoff.reason ? `说明：${handoff.reason}` : '',
          ].filter(Boolean).join('\n'),
        },
      )
      return
    }
    case 'desk_visit_tour': {
      if (sprintTerminal) return
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
      const update = message as Extract<OfficeActionMessage, { type: 'set_state' }>
      // Sprint 完成后会有后台复盘事件；不要让它把已结束团队重新标成工作中。
      // 独立产物生成与单兵任务有自己的起止事件，仍可准确显示负责成员状态。
      if (sprintTerminal && update.source !== 'artifact_generation' && update.source !== 'solo_task') return
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
      if (sprintTerminal) return
      const agentId = resolveAgentId(message)
      if (!agentId) {
        console.warn('[OfficeWS] emote: agent not found', message)
        return
      }
      playAgentEmote(agentId, message.animation, message.label)
      return
    }
    case 'solo_task_update': {
      // 单兵任务完成/失败：摘掉状态牌（保留其他进行中任务）、更新聊天任务卡片、成员回话
      const update = message as Extract<OfficeActionMessage, { type: 'solo_task_update' }>
      const rosterNo = Number(update.roster ?? 0)
      const agentId = getOfficeAgents()[rosterNo - 1]?.id
      if (agentId) {
        dropTaskLabel(agentId, `单飞·${String(update.title ?? '').slice(0, 16)}`)
        const remaining = activeTasksByAgent.get(agentId)
        if (remaining && remaining.size > 0) {
          const labels = [...remaining].slice(-2).join(' + ')
          setAgentState(
            agentId,
            'working',
            remaining.size > 2 ? `${labels}（共 ${remaining.size} 项）` : labels,
          )
        } else {
          activeTasksByAgent.delete(agentId)
          setAgentState(agentId, 'idle', '等待新任务')
        }
      }
      if (rosterNo && update.task_id) {
        updateTaskMessage(
          rosterNo,
          update.task_id,
          update.status === 'failed' ? 'failed' : 'done',
          update.artifact_id ?? null,
        )
        if (update.summary) {
          appendAssistantMessage(rosterNo, String(update.summary), update.status === 'failed' ? 'mock' : 'llm')
        }
      }
      const name = AGENT_ROSTER[rosterNo - 1]?.name ?? `#${rosterNo}`
      const title = String(update.title ?? '单飞任务')
      if (update.status === 'failed') {
        playTaskSound()
        showToast(`❌ ${name} 的单飞任务失败：${title}`)
        recordNotification(`${name} 的单飞任务失败：${title}`, 'solo_task_update', {
          title: `单飞任务失败 · ${title}`,
          detail: String(update.summary ?? ''),
        })
      } else {
        playTaskSound()
        pulseArtifacts()
        showToast(`✅ ${name} 完成单飞任务：${title}`, { kind: 'success' })
        recordNotification(`${name} 完成单飞任务：${title}`, 'solo_task_update', {
          title: `单飞任务完成 · ${title}`,
          detail: String(update.summary ?? ''),
        })
      }
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
        const taskLines = [
          `任务：${task.title ?? task.id ?? '未命名'}`,
          `状态：${task.status ?? '未知'}`,
          task.priority ? `优先级：${task.priority}` : '',
          task.assignee_roster ? `负责人：${AGENT_ROSTER[task.assignee_roster - 1]?.name ?? `成员 ${task.assignee_roster}`}` : '负责人：待分配',
          task.summary ? `说明：${task.summary}` : '',
          task.acceptance_criteria?.length ? `验收标准：\n${task.acceptance_criteria.map((criterion) => `• ${criterion}`).join('\n')}` : '',
        ].filter(Boolean).join('\n')
        recordNotification(statusText, 'task_update', { title: '任务状态详情', detail: taskLines })
        // 任务级结束提示：完成/打回时给一次轻量气泡与提示音，不必等整轮 Sprint 结束
        if (task.status === '通过') {
          playTaskSound()
          showToast(`✅ 任务完成：「${task.title ?? task.id}」`, { kind: 'success' })
        } else if (task.status === '打回') {
          playTaskSound()
          showToast(`↩ 「${task.title ?? task.id}」被打回，转交修复`)
        }
      }
      return
    }
    case 'sprint_start': {
      // 新 Sprint 开始：切回实时视角并刷新历史列表
      if (getViewedSprint()) void setViewedSprint('')
      void refreshSprintHistory()
      sprintTerminal = false
      activeTasksByAgent.clear()
      idleAllAgents()
      clearNotifications()
      clearThoughts()
      setSprintRunning(true)
      applyTaskSnapshot([])
      clearArtifacts()
      resetSprintMeta()
      const requirement = (message as { requirement?: unknown }).requirement
      const sprintId = (message as { sprint_id?: unknown }).sprint_id
      applySprintMeta({
        id: String(sprintId ?? ''),
        requirement: String(requirement ?? ''),
        status: 'running',
        stage: '准备开工',
        startedAt: Date.now() / 1000,
        finishedAt: 0,
      })
      const startText = `Sprint 开始：${String(requirement ?? '')}`
      // 把本轮 Sprint 归属到发起它的对话：新建对话后是干净窗口，老对话里仍能回看本轮卡片
      if (sprintId) markConversationSprint(0, getActiveConversationId(0), String(sprintId))
      pushActivity(startText)
      recordNotification(startText, 'sprint_start', {
        title: '本轮工作内容', detail: `需求：${String(requirement ?? '未填写')}\n状态：进行中\n阶段：准备开工`,
      })
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
      const viewed = getViewedSprint() || getSprintMetaSnapshot().id
      // 单飞产物（sprint_id='solo'）不入当前 Sprint 看板，但照常入产物 store 供交付面板刷新
      if (!artifact.sprint_id || !viewed || artifact.sprint_id === viewed || artifact.sprint_id === 'solo') upsertArtifact(artifact)
      void refreshSprintHistory()
      const recordKinds = new Set(['prd', 'dev', 'test', 'review', 'summary'])
      const record = recordKinds.has(artifact.kind)
      // 成品落地就点亮左侧「交付」徽标，不必等整轮 Sprint 完全结束
      if (!record) pulseArtifacts()
      const outcome = artifact.degraded
        ? `成品生成未成功，已保存失败说明：《${artifact.title}》`
        : !artifact.file_name && !record
          ? `产物说明已保存：《${artifact.title}》`
          : `最终产物已生成：《${artifact.title}》`
      recordNotification(
        record ? `新增工作记录：《${artifact.title}》` : outcome,
        record ? artifact.kind : 'final',
        { title: artifact.title, detail: artifactDetail(artifact) },
      )
      return
    }
    case 'artifact_update': {
      const artifact = (message as { artifact?: OfficeArtifact }).artifact
      if (!artifact) return
      const viewed = getViewedSprint() || getSprintMetaSnapshot().id
      if (!artifact.sprint_id || !viewed || artifact.sprint_id === viewed) upsertArtifact(artifact)
      void refreshSprintHistory()
      recordNotification(`工作记录已更新：《${artifact.title}》`, artifact.kind, {
        title: artifact.title,
        detail: artifactDetail(artifact),
      })
      return
    }
    case 'sprint_done': {
      void refreshSprintHistory()
      sprintTerminal = true
      setSprintRunning(false)
      idleAllAgents()
      // 终态防残留：实时动态只展示进行中的轮次，结束后清空，结果走通知中心/历史记录
      clearActivities()
      if (message.pending_checks === true) {
        applySprintMeta({ status: 'completed', stage: '交付待验证' })
        playTaskSound()
        pulseArtifacts()
        recordNotification('文件已生成，部分验收尚未执行', 'sprint_done', {
          title: '本轮验收状态', detail: finalQualityDetail(),
        })
        showToast('文件已生成，仍有未验证项，请查看产物的验收详情')
        return
      }
      if (message.accepted === false) {
        applySprintMeta({ status: 'failed', stage: '交付待修正' })
        playDoneSound()
        pulseArtifacts()
        recordNotification('交付待修正，请查看产物质检结果与复盘', 'sprint_error', {
          title: '验收失败详情', detail: finalQualityDetail(),
        })
        showToast('交付仍有未通过项，请到产物中心查看原因')
        notifySprintOutcome('error', getSprintMetaSnapshot().requirement)
        return
      }
      applySprintMeta({ status: 'completed', stage: '已完成' })
      playDoneSound()
      pulseArtifacts()
      recordNotification('Sprint 完成，团队进入空闲', 'sprint_done', {
        title: '本轮交付结果', detail: finalQualityDetail(),
      })
      showToast('🎉 Sprint 完成！最终交付物已生成，请到产物中心查看', {
        kind: 'success',
      })
      notifySprintOutcome('done', getSprintMetaSnapshot().requirement)
      return
    }
    case 'sprint_stopped': {
      sprintTerminal = true
      setSprintRunning(false)
      idleAllAgents()
      playTaskSound()
      pulseArtifacts()
      finalizeManualStop()
      recordNotification('任务已手动结束', 'sprint_done', {
        title: '结束时的交付状态', detail: finalQualityDetail(),
      })
      void refreshSprintHistory()
      showToast('任务已结束，团队进入空闲')
      return
    }
    case 'sprint_error': {
      sprintTerminal = true
      setSprintRunning(false)
      idleAllAgents()
      const errorMessage = (message as { message?: unknown }).message
      const errorText = `Sprint 异常：${String(errorMessage ?? '未知错误')}`
      applySprintMeta({ status: 'failed' })
      playDoneSound()
      clearActivities()
      recordNotification(errorText, 'sprint_error', {
        title: '错误详情', detail: String(errorMessage ?? '未知错误'),
      })
      showToast(errorText)
      notifySprintOutcome('error', getSprintMetaSnapshot().requirement)
      return
    }
    case 'sprint_end': {
      // 后端在每轮运行 finally 中发出的最后一个事件，作为终态收敛点：
      // 即使 sprint_done/stopped/error 因瞬断丢失，也能保证面板退出「进行中」。
      sprintTerminal = true
      const endPayload = message as { sprint_id?: unknown; status?: unknown; stage?: unknown }
      const wasRunning = isSprintRunning()
      setSprintRunning(false)
      idleAllAgents()
      // 手动结束的轮次以 sprint_stopped 的清理为准：不再回写 meta，仅做幂等兜底清理
      if (String(endPayload.status ?? '') === 'stopped') {
        finalizeManualStop()
        if (wasRunning) void refreshSprintHistory()
        return
      }
      // 只采纳与当前轮次匹配的 meta，忽略可能迟到的旧轮次信号
      if (!endPayload.sprint_id || String(endPayload.sprint_id) === getSprintMetaSnapshot().id) {
        applySprintMeta({
          ...(endPayload.status ? { status: String(endPayload.status) } : {}),
          ...(endPayload.stage ? { stage: String(endPayload.stage) } : {}),
        })
      }
      // 正常路径下 sprint_done 已刷新过历史；仅在遗漏终态事件时补拉
      if (wasRunning) void refreshSprintHistory()
      return
    }
    case 'plan_fallback': {
      const reason = (message as { reason?: unknown }).reason
      const text = `⚠ 任务拆解未按预期输出，已使用兜底拆解（${String(reason ?? '未知原因')}）`
      pushActivity(text)
      recordNotification(text, 'plan_fallback', {
        title: '拆解兜底原因', detail: String(reason ?? '未提供具体原因'),
      })
      showToast(text)
      return
    }
    case 'budget_exceeded': {
      sprintTerminal = true
      const text = `⚠ ${String((message as { message?: unknown }).message ?? '已达预算上限')}`
      setSprintRunning(false)
      idleAllAgents()
      applySprintMeta({ status: 'completed', stage: '已达预算上限' })
      playDoneSound()
      clearActivities()
      recordNotification(text, 'budget_exceeded', {
        title: '预算限制详情', detail: `当前需求：${getSprintMetaSnapshot().requirement || '未记录'}\n${text}`,
      })
      showToast(text)
      notifySprintOutcome('budget', getSprintMetaSnapshot().requirement)
      return
    }
    case 'sprint_paused': {
      setSprintPaused(true)
      const text = '已暂停：后端模型调用全部挂起，不再产生费用'
      pushActivity(text)
      recordNotification(text, 'sprint_paused', {
        title: '暂停说明', detail: `当前需求：${getSprintMetaSnapshot().requirement || '未记录'}\n模型调用已挂起。`,
      })
      showToast(text)
      return
    }
    case 'sprint_resumed': {
      setSprintPaused(false)
      pushActivity('已恢复：团队继续工作')
      recordNotification('已恢复：团队继续工作', 'sprint_resumed', {
        title: '恢复工作', detail: `当前需求：${getSprintMetaSnapshot().requirement || '未记录'}\n团队已继续处理本轮任务。`,
      })
      showToast('已恢复工作')
      return
    }
    case 'instruction_added': {
      const text = String((message as { text?: unknown }).text ?? '')
      const full = `📌 老板补充了指示${text ? `：${text}` : ''}`
      pushActivity(full)
      recordNotification(full, 'instruction_added', {
        title: '补充指示', detail: text || '未提供指示内容',
      })
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
      // 后端停止后仍保留旧任务/花费快照：手动结束的轮次不回填界面残留，历史走「任务」视图
      const snapshotStopped = !snapshot.running && snapshot.sprint?.status === 'stopped'
      if (Array.isArray(snapshot.tasks) && !getViewedSprint() && !snapshotStopped) {
        applyTaskSnapshot(snapshot.tasks as SprintTask[])
      }
      if (Array.isArray(snapshot.artifacts) && !getViewedSprint()) {
        setArtifacts(snapshot.artifacts as OfficeArtifact[])
      }
      // 正在浏览历史 Sprint：重连后重新拉取历史数据，避免被实时快照覆盖
      if (getViewedSprint()) void reloadViewedSprint()
      if (snapshot.sprint && !snapshotStopped) {
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
          cost: Number((snapshot.sprint as { cost?: unknown }).cost ?? 0),
        })
      }
      setSprintRunning(Boolean(snapshot.running))
      setSprintPaused(Boolean(snapshot.paused))
      sprintTerminal = !snapshot.running
      if (!snapshot.running) idleAllAgents()
      // 恢复通知中心/时间线：把持久化事件翻译回可读通知（不产生未读角标）
      if (Array.isArray(snapshot.events)) {
        const restored: { text: string; kind: string; time: string; title?: string; detail?: string }[] = []
        for (const event of snapshot.events) {
          const text = eventToText(event)
          if (!text) continue
          const artifact = (event.artifact ?? {}) as OfficeArtifact | undefined
          const ts = Number(event.ts ?? 0)
          const d = ts ? new Date(ts * 1000) : new Date()
          const hh = String(d.getHours()).padStart(2, '0')
          const mm = String(d.getMinutes()).padStart(2, '0')
          const ss = String(d.getSeconds()).padStart(2, '0')
          const detail = restoredEventDetail(event)
          restored.push({
            text,
            kind: artifact?.kind || String(event.type ?? ''),
            time: `${hh}:${mm}:${ss}`,
            title: detail.title,
            detail: detail.detail,
          })
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
