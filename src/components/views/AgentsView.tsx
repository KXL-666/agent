import { useEffect, useState, useSyncExternalStore } from 'react'
import { AgentConfigDialog } from '../AgentConfigDialog'
import { AgentProfileDialog } from '../AgentProfileDialog'
import { AGENT_PROFILES } from '@/data/agentProfiles'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'
import { setSelectedRoster } from '@/store/officeChatStore'
import {
  ensureAgentConfigsLoaded,
  fetchAgentConfigs,
  getAgentConfigsSnapshot,
  subscribeAgentConfigs,
} from '@/store/officeAgentConfigStore'
import { getOfficeAgents } from '@/store/officeStore'
import { setActiveView } from '@/store/officeViewStore'
import { AGENT_STATE_LABELS, hexColor } from './officeViewUtils'

export function AgentsView() {
  const [, forceTick] = useState(0)
  const [configFor, setConfigFor] = useState<number | null>(null)
  const [profileFor, setProfileFor] = useState<number | null>(null)
  const agentConfigs = useSyncExternalStore(
    subscribeAgentConfigs,
    getAgentConfigsSnapshot,
  )
  useEffect(() => {
    ensureAgentConfigsLoaded()
    const timer = setInterval(() => forceTick((n) => n + 1), 800)
    const healthTimer = setInterval(() => void fetchAgentConfigs(), 15000)
    return () => {
      clearInterval(timer)
      clearInterval(healthTimer)
    }
  }, [])

  function openChat(index: number) {
    setSelectedRoster(index + 1)
    setActiveView('chat')
  }

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>智能体</h1>
          <p>了解 6 名成员的职责、协作方式与交付内容，点击「详细介绍」查看更多</p>
        </div>
      </header>
      <div className="agent-grid">
        {AGENT_ROSTER.map((entry, index) => {
          const agent = getOfficeAgents()[index]
          const rosterNo = index + 1
          const cfg = agentConfigs[rosterNo]
          const displayName = cfg?.name || entry.name
          const avatarEmoji = cfg?.avatar || ''
          const availability = cfg?.availability ?? (cfg ? cfg.has_custom_key ? 'configured' : 'unconfigured' : undefined)
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
                <span className={`agent-state-chip state-${agent?.state ?? 'idle'}`}>
                  {AGENT_STATE_LABELS[agent?.state ?? 'idle'] ?? '空闲'}
                </span>
              </header>
              <p className="agent-profile-card-summary">{AGENT_PROFILES[index].summary}</p>
              <p className="agent-task">{agent?.currentTask || '暂无任务'}</p>
              <div className="agent-skill-row">
                {entry.skills.map((skill) => (
                  <span key={skill} className="view-chip small">{skill}</span>
                ))}
                {cfg?.has_custom_key ? (
                  <span className="view-chip small agent-custom-api">专属 API</span>
                ) : null}
                <span
                  className={`agent-availability-badge is-${availability ?? 'checking'}`}
                  title={cfg?.availability_reason || '状态每 15 秒更新；配置完成不代表刚刚通过连接测试'}
                >
                  {availability === 'unconfigured' ? '不可用 · 未配置' :
                    availability === 'error' ? '不可用 · 调用异常' :
                      availability === 'backup' ? '备用通道运行' :
                        availability === 'configured' ? '已配置' : '状态检查中'}
                </span>
                {cfg?.backup_in_use ? (
                  <span
                    className="view-chip small agent-backup-active"
                    title="主 API 连续失败，已自动切换备用通道"
                  >
                    ⚠ 备用生效中
                  </span>
                ) : cfg?.has_backup_key ? (
                  <span className="view-chip small agent-backup-api">有备用</span>
                ) : null}
              </div>
              <footer>
                <button type="button" aria-label={`查看${displayName}的详细介绍`} onClick={() => setProfileFor(rosterNo)}>
                  详细介绍
                </button>
                <button type="button" onClick={() => openChat(index)}>
                  找 TA 聊聊
                </button>
                <button
                  type="button"
                  className="agent-settings-btn"
                  onClick={() => setConfigFor(rosterNo)}
                >
                  ⚙ 设置
                </button>
              </footer>
            </article>
          )
        })}
      </div>
      {profileFor !== null ? (
        <AgentProfileDialog roster={profileFor}
          name={agentConfigs[profileFor]?.name || AGENT_ROSTER[profileFor - 1].name}
          avatar={agentConfigs[profileFor]?.avatar || ''}
          status={AGENT_STATE_LABELS[getOfficeAgents()[profileFor - 1]?.state ?? 'idle'] ?? '空闲'}
          task={getOfficeAgents()[profileFor - 1]?.currentTask || ''}
          onClose={() => setProfileFor(null)} onChangeMember={setProfileFor}
          onChat={() => { setProfileFor(null); openChat(profileFor - 1) }}
          onSkills={() => { setProfileFor(null); setActiveView('skills') }} />
      ) : null}
      {configFor !== null ? (
        <AgentConfigDialog
          config={
            agentConfigs[configFor] ?? {
              roster: configFor,
              name: AGENT_ROSTER[configFor - 1]?.name ?? '',
              role: AGENT_ROSTER[configFor - 1]?.role ?? '',
              llm: { base_url: '', api_key: '', model: '' },
              has_custom_key: false,
            }
          }
          onClose={() => setConfigFor(null)}
        />
      ) : null}
    </div>
  )
}
