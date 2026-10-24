/** 成员配置 store：自定义姓名 + 每个成员独立 LLM API（后端持久化） */

import { OFFICE_API_BASE } from '../config/officeMode'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'
import { renameAgent, setAgentAvatar, setAgentSkin } from '@/scene/officeSceneBridge'

export type AgentConfig = {
  roster: number
  name: string
  role: string
  /** 自定义头像 emoji，空串表示默认头像 */
  avatar?: string
  /** 自定义人物形象皮肤，空串表示按名册默认分配 */
  skin?: string
  /** 后端不回传真实 Key：api_key 恒为空，api_key_masked 仅用于提示 */
  llm: { base_url: string; api_key: string; api_key_masked?: string; model: string }
  /** 备用通道：主 API 连续失败时自动切换，结构与 llm 相同 */
  llm_backup?: { base_url: string; api_key: string; api_key_masked?: string; model: string }
  has_custom_key: boolean
  has_backup_key?: boolean
  /** 当前应答正走备用通道（主通道连续失败触发） */
  backup_in_use?: boolean
  availability?: 'unconfigured' | 'error' | 'backup' | 'configured'
  availability_reason?: string
  availability_updated_at?: number | null
}

/** 名册默认姓名快照（模块加载时留存，用于无自定义名时还原） */
const DEFAULT_NAMES = AGENT_ROSTER.map((entry) => entry.name)

let configs: Record<number, AgentConfig> = {}
let loaded = false

const listeners = new Set<() => void>()

function notify() {
  for (const listener of listeners) listener()
}

/** 把自定义姓名同步到名册与办公室场景名牌（任务中心/通知/拜访话术都读名册） */
function applyNamesEverywhere() {
  AGENT_ROSTER.forEach((entry, index) => {
    const roster = index + 1
    const name = configs[roster]?.name || DEFAULT_NAMES[index] || entry.name
    entry.name = name
    renameAgent(entry.id, name)
    setAgentAvatar(entry.id, configs[roster]?.avatar || '')
    setAgentSkin(entry.id, configs[roster]?.skin || '')
  })
}

export function subscribeAgentConfigs(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getAgentConfigsSnapshot(): Record<number, AgentConfig> {
  return configs
}

/** 成员显示名：有自定义配置时覆盖名册原名 */
export function agentDisplayName(roster: number, fallback: string): string {
  return configs[roster]?.name || fallback
}

/** 成员自定义头像 emoji：未设置时返回空串（渲染端保持默认头像） */
export function agentAvatarEmoji(roster: number): string {
  return configs[roster]?.avatar || ''
}

export async function fetchAgentConfigs(): Promise<void> {
  try {
    const res = await fetch(`${OFFICE_API_BASE}/api/agents`)
    if (!res.ok) return
    const payload = (await res.json()) as { agents?: AgentConfig[] }
    configs = Object.fromEntries(
      (payload.agents ?? []).map((agent) => [agent.roster, agent]),
    )
    loaded = true
    applyNamesEverywhere()
    notify()
  } catch {
    // 后端未启动时保持现状
  }
}

export function ensureAgentConfigsLoaded(): void {
  if (!loaded) void fetchAgentConfigs()
}

export type AgentConfigPatch = {
  name?: string
  avatar?: string
  skin?: string
  base_url?: string
  api_key?: string
  model?: string
  backup_base_url?: string
  /** 空串不提交=保留旧值；"__CLEAR__"=清除 */
  backup_api_key?: string
  backup_model?: string
}

export async function saveAgentConfig(
  roster: number,
  patch: AgentConfigPatch,
): Promise<AgentConfig> {
  const res = await fetch(`${OFFICE_API_BASE}/api/agents/${roster}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) throw new Error(`保存失败：${res.status}`)
  const updated = (await res.json()) as AgentConfig
  configs = { ...configs, [roster]: updated }
  applyNamesEverywhere()
  notify()
  return updated
}
