import { useEffect, useState, useSyncExternalStore } from 'react'
import { SkillsManager } from '../SkillsManager'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'
import {
  ensureAgentConfigsLoaded,
  getAgentConfigsSnapshot,
  subscribeAgentConfigs,
} from '@/store/officeAgentConfigStore'
import { hexColor } from './officeViewUtils'

type HealthPayload = { ok: boolean; running: boolean; llm: boolean; ws_clients: number }

type AgentApiInfo = {
  roster: number
  name: string
  role: string
  has_custom_key: boolean
  llm?: { model?: string; base_url?: string }
}

export function SkillsView() {
  const [health, setHealth] = useState<HealthPayload | null>(null)
  const [agents, setAgents] = useState<AgentApiInfo[]>([])
  // 订阅成员配置：自定义头像/姓名保存后无需刷新即可反映到名册卡片
  const agentConfigs = useSyncExternalStore(
    subscribeAgentConfigs,
    getAgentConfigsSnapshot,
  )

  useEffect(() => {
    ensureAgentConfigsLoaded()
    let cancelled = false
    fetch(`${OFFICE_API_BASE}/api/health`)
      .then((res) => res.json())
      .then((data: HealthPayload) => {
        if (!cancelled) setHealth(data)
      })
      .catch(() => undefined)
    fetch(`${OFFICE_API_BASE}/api/agents`)
      .then((res) => res.json())
      .then((data: { agents?: AgentApiInfo[] }) => {
        if (!cancelled) setAgents(data.agents ?? [])
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [])

  const configured = agents.filter((a) => a.has_custom_key)
  const unconfigured = agents.filter((a) => !a.has_custom_key)
  const llmLabel =
    agents.length === 0
      ? health == null
        ? '检测中…'
        : health.llm
          ? '已接入（真实生成）'
          : 'Mock 模式（未配置任何 LLM API）'
      : configured.length === 0
        ? 'Mock 模式（全员未配置专属 API）'
        : unconfigured.length === 0
          ? `全员已配置专属 API（${configured.length}/${agents.length}）`
          : `已配置专属 API：${configured.length}/${agents.length} 名成员，其余 Mock 兜底`

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>技能配置</h1>
          <p>配置工作技能，让成员在执行任务时使用专业方法与规范</p>
        </div>
      </header>

      <SkillsManager names={AGENT_ROSTER.map((entry, index) => agentConfigs[index + 1]?.name || entry.name)} />

      <section className="view-card">
        <h2>系统引擎</h2>
        <ul className="skill-engine-list">
          <li>
            <span>LLM 决策</span>
            <strong className={configured.length > 0 || health?.llm ? 'ok' : 'warn'}>
              {llmLabel}
            </strong>
          </li>
          {configured.length > 0 ? (
            <li>
              <span>已配置成员</span>
              <strong className="ok">
                {configured
                  .map((a) => `${a.name}${a.llm?.model ? `（${a.llm.model}）` : ''}`)
                  .join('、')}
              </strong>
            </li>
          ) : null}
          {unconfigured.length > 0 && agents.length > 0 ? (
            <li>
              <span>未配置成员</span>
              <strong className="warn">
                {unconfigured.map((a) => a.name).join('、')}（可在「团队 → 成员」配置）
              </strong>
            </li>
          ) : null}
          <li>
            <span>流程编排</span>
            <strong className="ok">LangGraph 状态机（拆解→开发→测试→评审）</strong>
          </li>
          <li>
            <span>实时通信</span>
            <strong className="ok">
              WebSocket 推送（{health ? `${health.ws_clients} 个客户端在线` : '—'}）
            </strong>
          </li>
          <li>
            <span>Sprint 状态</span>
            <strong className={health?.running ? 'run' : 'ok'}>
              {health == null ? '—' : health.running ? '进行中' : '空闲'}
            </strong>
          </li>
        </ul>
      </section>

      <div className="agent-grid">
        {AGENT_ROSTER.map((entry, index) => {
          const cfg = agentConfigs[index + 1]
          const displayName = cfg?.name || entry.name
          const avatarEmoji = cfg?.avatar || ''
          return (
          <article key={entry.id} className="agent-card">
            <header>
              <span
                className="agent-avatar"
                style={{ background: hexColor(entry.color) }}
              >
                {avatarEmoji ? (
                  <span className="agent-avatar-emoji">{avatarEmoji}</span>
                ) : (
                  displayName.slice(0, 1)
                )}
              </span>
              <div>
                <strong>{displayName}</strong>
                <em>{entry.role}</em>
              </div>
            </header>
            <div className="agent-skill-row">
              {entry.skills.map((skill) => (
                <span key={skill} className="view-chip small">{skill}</span>
              ))}
            </div>
          </article>
          )
        })}
      </div>
    </div>
  )
}
