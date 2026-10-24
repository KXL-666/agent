import { useCallback, useEffect, useState, useSyncExternalStore, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { OfficeChatView } from '@/components/OfficeChatView'
import { ArtifactQualityPanel } from '@/components/ArtifactQualityPanel'
import { UploadedKnowledge } from '@/components/UploadedKnowledge'
import { SkillsManager } from '@/components/SkillsManager'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { captureOfficeScreenshot } from '@/scene/officeSceneBridge'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'
import { setSelectedRoster } from '@/store/officeChatStore'
import {
  getArtifactsSnapshot,
  removeArtifact,
  setArtifacts,
  subscribeArtifacts,
  upsertArtifact,
  type OfficeArtifact,
} from '@/store/officeArtifactStore'
import { AgentConfigDialog } from './AgentConfigDialog'
import { AgentProfileDialog } from './AgentProfileDialog'
import { AGENT_PROFILES } from '@/data/agentProfiles'
import {
  ensureAgentConfigsLoaded,
  fetchAgentConfigs,
  getAgentConfigsSnapshot,
  subscribeAgentConfigs,
} from '@/store/officeAgentConfigStore'
import {
  clearNotifications,
  getNotificationsSnapshot,
  markNotificationsRead,
  subscribeNotifications,
} from '@/store/officeEventStore'
import { getOfficeAgents } from '@/store/officeStore'
import {
  assigneeName,
  getSprintMetaSnapshot,
  getTasksSnapshot,
  isSprintRunning,
  statusMeta,
  displayTaskMeta,
  subscribeSprintData,
  type SprintTask,
} from '@/store/officeTaskStore'
import { showToast } from '@/store/officeUiStore'
import {
  WORKSPACES,
  getActiveWorkspace,
  getViewedSprint,
  getSprintHistory,
  openSprintDialog,
  setActiveView,
  setActiveWorkspace,
  setViewedSprint,
  subscribeView,
  workspaceMatches,
  type OfficeView,
} from '@/store/officeViewStore'
import { TaskDetailDialog } from './OfficeDashboardChrome'

const AGENT_STATE_LABELS: Record<string, string> = {
  idle: '空闲',
  walking: '走动中',
  working: '工作中',
  talking: '沟通中',
  thinking: '思考中',
}

function hexColor(color: number): string {
  return `#${color.toString(16).padStart(6, '0')}`
}

function timeLabel(ts: number): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

/* ------------------------------- 任务中心 ------------------------------- */

const STATUS_ORDER = ['待分派', '待开发', '待测试', '待评审', '打回', '通过']

function WideTaskCard({ task, onOpen }: { task: SprintTask; onOpen?: () => void }) {
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const meta = displayTaskMeta(task.status, running)
  const cancelled = task.status === '已取消'
  return (
    <article
      className="agent-card task-card-clickable"
      role={onOpen ? 'button' : undefined}
      onClick={onOpen}
    >
      <header>
        <div>
          <strong className={cancelled ? 'task-title-cancelled' : undefined}>
            {task.title || task.id}
          </strong>
          <em>
            {assigneeName(task.assignee_roster)} · {task.id}
            {task.priority ? ` · ${task.priority} · ${task.story_points ?? 3} 点` : ''}
          </em>
        </div>
        <span
          className={`agent-state-chip ${
            cancelled ? 'state-cancelled' : meta.done ? 'state-idle' : 'state-working'
          }`}
        >
          {meta.label}
        </span>
      </header>
      <p className="agent-task">{task.summary || '暂无描述'}</p>
      <div className="task-progress">
        <span
          className={`tone-${meta.tone}`}
          style={{ width: `${meta.progress}%` }}
        />
      </div>
      <em className="task-percent">{meta.progress}%</em>
    </article>
  )
}

function TaskCenterView() {
  const tasks = useSyncExternalStore(subscribeSprintData, getTasksSnapshot)
  const sprint = useSyncExternalStore(subscribeSprintData, getSprintMetaSnapshot)
  const workspaceIndex = useSyncExternalStore(subscribeView, getActiveWorkspace)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const history = useSyncExternalStore(subscribeView, getSprintHistory)
  const [detailTask, setDetailTask] = useState<SprintTask | null>(null)
  const workspace = WORKSPACES[workspaceIndex]
  const filtered = tasks.filter((task) =>
    workspaceMatches(task.assignee_roster),
  )
  const active = filtered.filter((task) => task.status !== '已取消')
  const totalPoints = active.reduce((sum, task) => sum + (task.story_points ?? 0), 0)
  const donePoints = active.filter((task) => task.status === '通过').reduce((sum, task) => sum + (task.story_points ?? 0), 0)
  const estimated = active.length > 0 && active.every((task) => Number.isFinite(task.story_points) && (task.story_points ?? 0) > 0)
  const doneCount = active.filter((task) => task.status === '通过').length
  const columns = [...STATUS_ORDER, ...(filtered.some((task) => task.status === '已取消') ? ['已取消'] : [])]

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>任务</h1>
          <p>
            迭代计划 · 用户故事 · 测试验收 · 复盘改进
            {workspaceIndex > 0 ? ` · ${workspace.label}` : ''}
          </p>
        </div>
        <button
          type="button"
          className="view-primary-btn new-sprint-btn"
          onClick={() => openSprintDialog()}
        >
          + 新建 Sprint
        </button>
      </header>

      {viewedSprint ? (
        <div className="view-history-banner">
          正在查看历史 Sprint（只读），实时进展请
          <button type="button" onClick={() => void setViewedSprint('')}>
            返回当前 Sprint
          </button>
        </div>
      ) : null}

      <div className="task-filter" role="group" aria-label="按负责人筛选任务">
        <span>负责人</span>
        {WORKSPACES.map((option, index) => (
          <button key={option.label} type="button" className={workspaceIndex === index ? 'active' : ''} aria-pressed={workspaceIndex === index} onClick={() => setActiveWorkspace(index)}>
            {option.label}
          </button>
        ))}
      </div>

      <section className="view-card agile-overview" aria-label="迭代目标与进度">
        <h2>本轮迭代目标</h2>
        <p>{(viewedSprint ? history.find((item) => item.id === viewedSprint)?.requirement : sprint.requirement) || '创建 Sprint 后，将需求转成有优先级、工作量与验收条件的迭代计划。'}</p>
        <div className="view-chips">
          {estimated ? (
            <>
              <span className="view-chip">计划工作量 <strong>{totalPoints} 点</strong></span>
              <span className="view-chip">已通过 <strong>{donePoints} 点</strong></span>
              <span className="view-chip">剩余工作量 <strong>{totalPoints - donePoints} 点</strong></span>
            </>
          ) : (
            <>
              <span className="view-chip">本轮任务 <strong>{active.length} 项</strong></span>
              <span className="view-chip">已通过 <strong>{doneCount} 项</strong></span>
              <span className="view-chip">剩余 <strong>{active.length - doneCount} 项</strong></span>
            </>
          )}
          <span className="view-chip">待修复 <strong>{active.filter((task) => task.status === '打回').length}</strong></span>
          {!estimated && active.length > 0 ? <span className="view-chip">未完整估点 <strong>{active.filter((task) => !task.story_points).length} 项</strong></span> : null}
        </div>
        <progress aria-label={estimated ? '已通过故事点比例' : '已通过任务比例'} max={estimated ? totalPoints : active.length || 1} value={estimated ? donePoints : doneCount} />
        <p className="view-card-meta">{estimated ? '故事点表示相对工作量；' : '任务未完整估点，当前按任务数量显示进度；'}仅通过测试与评审的任务计入完成。点击任务查看用户故事和验收条件。最终文件另有成品质检。</p>
      </section>

      <div className="view-chips">
        {STATUS_ORDER.map((status) => (
          <span key={status} className="view-chip">
            {status}
            <strong>{filtered.filter((t) => t.status === status).length}</strong>
          </span>
        ))}
      </div>

      {filtered.length === 0 ? (
        <div className="view-empty">
          {tasks.length === 0
            ? '还没有任务。点击「新建 Sprint」描述需求，产品经理会自动拆解。'
            : '当前筛选条件下没有任务，可切换到「全部」查看。'}
        </div>
      ) : (
        <div className="agile-board">
          {columns.map((status) => (
            <section className="agile-column" key={status} aria-label={status}>
              <h2>{status} <small>{filtered.filter((task) => task.status === status).length}</small></h2>
              {filtered.filter((task) => task.status === status).map((task) => (
                <WideTaskCard key={task.id} task={task} onOpen={() => setDetailTask(task)} />
              ))}
              {!filtered.some((task) => task.status === status) ? <p className="view-card-meta">暂无任务</p> : null}
            </section>
          ))}
        </div>
      )}
      {detailTask ? (
        <TaskDetailDialog task={detailTask} onClose={() => setDetailTask(null)} />
      ) : null}
    </div>
  )
}

/* -------------------------------- 智能体 -------------------------------- */

function AgentsView() {
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

/* ------------------------------- 文件中心 ------------------------------- */

type FilesPayload = {
  requirement: string
  running: boolean
  updated_at: number
  tasks: SprintTask[]
  artifacts: OfficeArtifact[]
  report_md: string
}

const ARTIFACT_KIND_LABELS: Record<OfficeArtifact['kind'], string> = {
  prd: 'PRD',
  dev: '开发',
  test: '测试',
  review: '评审',
  summary: '总结',
  custom: '生成',
  final: '交付物',
}

/** 有真实文件时走后端下载，否则退回 Markdown Blob */
function downloadArtifact(artifact: OfficeArtifact) {
  if (artifact.file_name) {
    const anchor = document.createElement('a')
    anchor.href = `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/download`
    anchor.download = artifact.file_name.split(/[\\/]/).pop() || artifact.title
    anchor.click()
    showToast('真实文件已开始下载')
    return
  }
  const blob = new Blob([artifact.content], {
    type: 'text/markdown;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${artifact.title}.md`
  anchor.click()
  URL.revokeObjectURL(url)
  showToast('产物已下载')
}

/** 从 file_name 取扩展名用于徽标展示 */
function fileExt(artifact: OfficeArtifact): string | null {
  if (!artifact.file_name) return null
  const dot = artifact.file_name.lastIndexOf('.')
  return dot >= 0 ? artifact.file_name.slice(dot) : null
}

function sourceLabel(artifact: OfficeArtifact, legacyDemo: boolean): string {
  const source = (!artifact.source || artifact.source === 'unknown') && legacyDemo ? 'demo' : artifact.source
  switch (source) {
    case 'demo': return '脚本演示'
    case 'model': return '真实模型'
    case 'fallback': return '兜底模板'
    case 'local': return '本地处理'
    default: return '来源未记录'
  }
}

function qualityLabel(artifact: OfficeArtifact): string {
  switch (artifact.quality_status) {
    case 'passed': return '自动验收通过'
    case 'failed': return '存在验收问题'
    case 'unchecked': return '部分项目待验证'
    default: return '暂无验收记录'
  }
}

function canPreviewFile(artifact: OfficeArtifact): boolean {
  return /\.(docx|pptx|xlsx|pdf)$/i.test(artifact.file_name ?? '')
}

function ArtifactCard({ artifact }: { artifact: OfficeArtifact }) {
  const ext = fileExt(artifact)
  return (
    <details className="artifact-card">
      <summary>
        <span className={`artifact-kind kind-${artifact.kind}`}>
          {ARTIFACT_KIND_LABELS[artifact.kind] ?? '产物'}
        </span>
        <strong>{artifact.title}</strong>
        {ext ? <span className="artifact-file-badge">{ext}</span> : null}
        <em>
          {assigneeName(artifact.author_roster)}
          {artifact.task_id ? ` · ${artifact.task_id}` : ''}
        </em>
      </summary>
      <pre className="view-md artifact-content">{artifact.content}</pre>
      {artifact.file_name ? (
        <footer className="artifact-card-actions">
          <button type="button" onClick={() => downloadArtifact(artifact)}>
            下载 {ext}
          </button>
        </footer>
      ) : null}
    </details>
  )
}

function FilesView() {
  const [data, setData] = useState<FilesPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const artifacts = useSyncExternalStore(
    subscribeArtifacts,
    getArtifactsSnapshot,
  )

  const load = useCallback(async () => {
    setLoading(true)
    try {
      if (!viewedSprint) {
        const res = await fetch(`${OFFICE_API_BASE}/api/files`)
        if (!res.ok) throw new Error(String(res.status))
        const payload = (await res.json()) as FilesPayload
        setData(payload)
        setArtifacts(payload.artifacts ?? [])
      } else {
        // 历史 Sprint：无日报，需求/任务/产物取自详情接口
        const res = await fetch(`${OFFICE_API_BASE}/api/sprints/${viewedSprint}`)
        if (!res.ok) throw new Error(String(res.status))
        const payload = (await res.json()) as {
          sprint?: { requirement?: string; started_at?: number }
          tasks?: SprintTask[]
          artifacts?: OfficeArtifact[]
        }
        setData({
          requirement: payload.sprint?.requirement ?? '',
          running: false,
          updated_at: payload.sprint?.started_at ?? 0,
          tasks: payload.tasks ?? [],
          artifacts: payload.artifacts ?? [],
          report_md: '',
        })
        setArtifacts(payload.artifacts ?? [])
      }
      setError(null)
    } catch {
      setError('无法连接后端，请确认服务已启动')
    } finally {
      setLoading(false)
    }
  }, [viewedSprint])

  useEffect(() => {
    void load()
  }, [load])

  function downloadReport() {
    if (!data?.report_md) return
    const blob = new Blob([data.report_md], {
      type: 'text/markdown;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'sprint-report.md'
    anchor.click()
    URL.revokeObjectURL(url)
    showToast('Sprint 日报已下载')
  }

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>过程文件</h1>
          <p>Sprint 产物（PRD / 开发说明 / 测试报告 / 评审记录 / 总结）、任务清单与日报</p>
        </div>
        <div className="view-head-actions">
          <button type="button" className="view-ghost-btn" onClick={load}>
            {loading ? '刷新中…' : '刷新'}
          </button>
          <button
            type="button"
            className="view-primary-btn"
            onClick={downloadReport}
            disabled={!data?.report_md}
          >
            下载日报
          </button>
        </div>
      </header>

      {error ? <div className="view-empty">{error}</div> : null}

      {!error && data ? (
        data.requirement || data.tasks.length > 0 || artifacts.length > 0 ? (
          <>
            <section className="view-card">
              <h2>需求描述</h2>
              <p className="view-card-text">
                {data.requirement || '（未记录）'}
              </p>
              <p className="view-card-meta">
                状态：{data.running ? 'Sprint 进行中' : '空闲/已完成'} ·
                更新于 {timeLabel(data.updated_at)}
              </p>
            </section>

            <section className="view-card">
              <h2>Sprint 产物（{artifacts.length}）</h2>
              {artifacts.length === 0 ? (
                <p className="view-card-meta">
                  暂无产物。Sprint 运行中，各环节（拆解 / 开发 / 测试 / 评审 / 复盘）完成后会实时出现在这里。
                </p>
              ) : (
                <>
                  <div className="artifact-list">
                    {artifacts.map((artifact) => (
                      <ArtifactCard key={artifact.id} artifact={artifact} />
                    ))}
                  </div>
                  <p className="view-card-meta">
                    <a
                      href={`${OFFICE_API_BASE}/api/artifacts/code.zip`}
                    >
                      下载本次 Sprint 全部代码（zip）
                    </a>
                  </p>
                </>
              )}
            </section>

            <section className="view-card">
              <h2>任务清单（{data.tasks.length}）</h2>
              {data.tasks.length === 0 ? (
                <p className="view-card-meta">暂无任务</p>
              ) : (
                <ul className="view-task-rows">
                  {data.tasks.map((task) => {
                    const meta = statusMeta(task.status)
                    return (
                      <li key={task.id}>
                        <span className={`tone-dot ${meta.tone}`} />
                        <strong>{task.title || task.id}</strong>
                        <em>{assigneeName(task.assignee_roster)}</em>
                        <span>{task.status}</span>
                      </li>
                    )
                  })}
                </ul>
              )}
            </section>

            <section className="view-card">
              <h2>Sprint 日报预览</h2>
              <pre className="view-md">{data.report_md}</pre>
            </section>
          </>
        ) : (
          <div className="view-empty">
            还没有 Sprint 产物。发起一次 Sprint 后，需求、任务、各环节产物与日报会出现在这里。
          </div>
        )
      ) : null}
    </div>
  )
}

function KnowledgeView() {
  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>参考资料</h1>
          <p>管理上传的资料，供团队在任务中查阅。</p>
        </div>
      </header>
      <UploadedKnowledge />
    </div>
  )
}

/* ------------------------------- 技能中心 ------------------------------- */

type HealthPayload = { ok: boolean; running: boolean; llm: boolean; ws_clients: number }

type AgentApiInfo = {
  roster: number
  name: string
  role: string
  has_custom_key: boolean
  llm?: { model?: string; base_url?: string }
}

function SkillsView() {
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

/* ----------------------------- 浏览器助手 ----------------------------- */

function BrowserView() {
  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>浏览器助手</h1>
          <p>打开与本项目相关的在线资源（新标签页）</p>
        </div>
      </header>
      <div className="browser-grid">
        <a
          className="browser-card"
          href={`${OFFICE_API_BASE}/docs`}
          target="_blank"
          rel="noreferrer"
        >
          <strong>后端 API 文档</strong>
          <p>FastAPI 自动生成的交互式接口文档（Swagger UI）</p>
          <em>{OFFICE_API_BASE}/docs</em>
        </a>
        <a
          className="browser-card"
          href="https://github.com/KXL-666/ai-office-react"
          target="_blank"
          rel="noreferrer"
        >
          <strong>前端场景源码仓库</strong>
          <p>ai-office-react：PixiJS + Spine 的办公室可视化舞台</p>
          <em>github.com/KXL-666/ai-office-react</em>
        </a>
        <button
          type="button"
          className="browser-card"
          onClick={() => {
            const dataUrl = captureOfficeScreenshot()
            if (!dataUrl) {
              showToast('画布尚未就绪，请先回到首页')
              return
            }
            const anchor = document.createElement('a')
            anchor.href = dataUrl
            anchor.download = `office-snapshot-${Date.now()}.png`
            anchor.click()
            showToast('办公室画布截图已导出')
          }}
        >
          <strong>导出画布截图</strong>
          <p>把当前办公室画面保存为 PNG 图片</p>
          <em>无需联网，本地即时生成</em>
        </button>
      </div>
    </div>
  )
}

/* ------------------------------- 日程日历 ------------------------------- */

function CalendarView() {
  const notifications = useSyncExternalStore(
    subscribeNotifications,
    getNotificationsSnapshot,
  )
  const tasks = useSyncExternalStore(subscribeSprintData, getTasksSnapshot)
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)

  const milestones = [
    { label: '需求拆解', done: tasks.length > 0 },
    {
      label: '开发完成',
      done: tasks.some((t) => ['待测试', '打回', '通过'].includes(t.status)),
    },
    { label: '测试通过', done: tasks.some((t) => t.status === '通过') },
  ]

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>里程碑与动态</h1>
          <p>Sprint 里程碑与团队动态时间线</p>
        </div>
      </header>

      <section className="view-card">
        <h2>
          本轮里程碑
          {running ? '（Sprint 进行中）' : ''}
        </h2>
        <div className="milestone-row">
          {milestones.map((item) => (
            <span
              key={item.label}
              className={`milestone ${item.done ? 'done' : ''}`}
            >
              {item.done ? '✓' : '○'} {item.label}
            </span>
          ))}
        </div>
      </section>

      <section className="view-card">
        <h2>动态时间线</h2>
        {notifications.length === 0 ? (
          <p className="view-card-meta">
            暂无记录。发起 Sprint 后，团队的每次交接与任务流转都会按时间记录在这里。
          </p>
        ) : (
          <ul className="timeline-list">
            {notifications.map((item) => (
              <li key={item.id}>
                <span className="timeline-dot" />
                <div>
                  <p>{item.text}</p>
                  <time>{item.time}</time>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

/* ------------------------------- 通知中心 ------------------------------- */

const KIND_LABELS: Record<string, string> = {
  sprint_start: 'Sprint',
  sprint_done: 'Sprint',
  sprint_error: '异常',
  desk_visit: '沟通',
  desk_visit_tour: '会议',
  task_update: '任务',
  artifact_new: '产物',
  instruction_added: '指示',
  prd: '需求',
  dev: '开发',
  test: '测试',
  review: '评审',
  summary: '复盘',
  final: '成品',
  custom: '成品',
}

function NotificationsView() {
  const notifications = useSyncExternalStore(
    subscribeNotifications,
    getNotificationsSnapshot,
  )

  useEffect(() => {
    markNotificationsRead()
  }, [])

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>通知</h1>
          <p>后端实时推送的团队事件（WebSocket）</p>
        </div>
        <button
          type="button"
          className="view-ghost-btn"
          onClick={() => {
            clearNotifications()
            showToast('通知已清空')
          }}
        >
          清空通知
        </button>
      </header>

      {notifications.length === 0 ? (
        <div className="view-empty">暂无通知，等待团队动态。</div>
      ) : (
        <ul className="notification-list">
          {notifications.map((item) => (
            <li key={item.id}>
              <span className={`notification-kind kind-${item.kind}`}>
                {KIND_LABELS[item.kind] ?? '事件'}
              </span>
              <p>{item.text}</p>
              {item.detail ? (
                <details className="notification-record">
                  <p className="notification-detail-preview">
                    <strong>{item.title || '详细信息'}：</strong>{item.detail.split('\n').slice(0, 4).join(' · ')}
                  </p>
                  <summary>展开完整详情</summary>
                  <pre>{item.detail}</pre>
                </details>
              ) : null}
              <time>{item.time}</time>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/* ------------------------------ 产物中心 ------------------------------ */

type UsageMember = {
  roster: number
  name?: string
  model: string
  calls: number
  prompt_tokens: number
  completion_tokens: number
  cost: number
}

type UsageReport = {
  members: UsageMember[]
  total_cost: number
  estimated: boolean
}

type PricingPayload = {
  prices: Record<string, [number, number]>
  default: [number, number]
  builtin: string[]
}

/** 单价编辑行：reset=true 表示保存时提交 null 恢复内置价 */
type PriceRow = { model: string; input: string; output: string; reset: boolean }

function UsagePanel() {
  const [usage, setUsage] = useState<UsageReport | null>(null)
  const sprint = useSyncExternalStore(subscribeSprintData, getSprintMetaSnapshot)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const history = useSyncExternalStore(subscribeView, getSprintHistory)
  const [priceRows, setPriceRows] = useState<PriceRow[]>([])
  const [pricingLoaded, setPricingLoaded] = useState(false)
  const [pricingSaving, setPricingSaving] = useState(false)

  async function loadUsage() {
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/usage`)
      if (!res.ok) return
      setUsage((await res.json()) as UsageReport)
    } catch {
      /* 用量面板静默失败，不影响主流程 */
    }
  }

  useEffect(() => {
    loadUsage()
  }, [])

  async function loadPricing() {
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/pricing`)
      if (!res.ok) return
      const payload = (await res.json()) as PricingPayload
      setPriceRows(
        Object.entries(payload.prices ?? {}).map(([model, pair]) => ({
          model,
          input: String(pair[0]),
          output: String(pair[1]),
          reset: false,
        })),
      )
      setPricingLoaded(true)
    } catch {
      showToast('加载单价配置失败')
    }
  }

  function updateRow(index: number, patch: Partial<PriceRow>) {
    setPriceRows((rows) =>
      rows.map((row, i) => (i === index ? { ...row, ...patch } : row)),
    )
  }

  async function savePricing() {
    if (pricingSaving) return
    const prices: Record<string, [number, number] | null> = {}
    for (const row of priceRows) {
      const model = row.model.trim()
      if (!model) continue
      if (row.reset) {
        prices[model] = null
        continue
      }
      const input = Number(row.input)
      const output = Number(row.output)
      if (!Number.isFinite(input) || !Number.isFinite(output)) continue
      prices[model] = [input, output]
    }
    setPricingSaving(true)
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/pricing`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prices }),
      })
      if (!res.ok) {
        const detail = (await res.json().catch(() => null)) as {
          detail?: string
        } | null
        showToast(detail?.detail ?? '保存单价失败')
        return
      }
      showToast('单价已保存，金额已按新价重算')
      await loadPricing()
      await loadUsage()
    } catch {
      showToast('保存单价失败，请确认后端已启动')
    } finally {
      setPricingSaving(false)
    }
  }

  const totals = usage?.members.reduce(
    (acc, m) => ({
      calls: acc.calls + m.calls,
      prompt: acc.prompt + m.prompt_tokens,
      completion: acc.completion + m.completion_tokens,
    }),
    { calls: 0, prompt: 0, completion: 0 },
  )
  const viewedCost = viewedSprint
    ? history.find((item) => item.id === viewedSprint)?.usage_cost
    : sprint.id ? sprint.cost : undefined

  return (
    <details
      className="usage-panel"
      onToggle={(event) => {
        if (event.currentTarget.open) loadUsage()
      }}
    >
      <summary>
        <span>费用统计（估算）</span>
        <span className="usage-total">
          {viewedCost == null ? '本次费用暂无记录' : `${viewedSprint ? '所选历史' : '当前'} Sprint ¥${viewedCost.toFixed(4)}`}
          {' · '}
          {usage ? `所有历史累计 ¥${usage.total_cost.toFixed(4)}` : '历史累计加载中'}
        </span>
      </summary>
      {usage && usage.members.length > 0 && totals ? (
        <>
          <table>
            <thead>
              <tr>
                <th>成员</th>
                <th>模型</th>
                <th>调用次数</th>
                <th>输入 tokens</th>
                <th>输出 tokens</th>
                <th>估算花费</th>
              </tr>
            </thead>
            <tbody>
              {usage.members.map((m) => (
                <tr key={m.roster}>
                  <td>{m.name ?? `#${m.roster}`}</td>
                  <td>{m.model}</td>
                  <td>{m.calls}</td>
                  <td>{m.prompt_tokens}</td>
                  <td>{m.completion_tokens}</td>
                  <td>¥{m.cost.toFixed(4)}</td>
                </tr>
              ))}
              <tr className="usage-total-row">
                <td>所有历史合计</td>
                <td>—</td>
                <td>{totals.calls}</td>
                <td>{totals.prompt}</td>
                <td>{totals.completion}</td>
                <td>¥{usage.total_cost.toFixed(4)}</td>
              </tr>
            </tbody>
          </table>
          <div className="usage-note">
            上表为所有历史模型调用的累计用量；本次 Sprint 花费见标题。费用按各服务商公开刊例约值估算，以实际账单为准。
            <a
              className="usage-export-link"
              href={`${OFFICE_API_BASE}/api/usage/export.csv`}
              download
            >
              ⇩ 导出 CSV
            </a>
          </div>
          <details
            className="pricing-panel"
            onToggle={(event) => {
              if (event.currentTarget.open && !pricingLoaded) void loadPricing()
            }}
          >
            <summary>自定义单价（元/百万tokens）</summary>
            {pricingLoaded ? (
              <>
                <table>
                  <thead>
                    <tr>
                      <th>模型</th>
                      <th>输入价</th>
                      <th>输出价</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {priceRows.map((row, index) => (
                      <tr key={index} className={row.reset ? 'pricing-row-reset' : ''}>
                        <td>
                          <input
                            value={row.model}
                            placeholder="模型名，如 gpt-4o"
                            disabled={row.reset}
                            onChange={(event) =>
                              updateRow(index, { model: event.target.value })
                            }
                          />
                        </td>
                        <td>
                          <input
                            type="number"
                            min="0"
                            step="0.01"
                            value={row.input}
                            disabled={row.reset}
                            onChange={(event) =>
                              updateRow(index, { input: event.target.value })
                            }
                          />
                        </td>
                        <td>
                          <input
                            type="number"
                            min="0"
                            step="0.01"
                            value={row.output}
                            disabled={row.reset}
                            onChange={(event) =>
                              updateRow(index, { output: event.target.value })
                            }
                          />
                        </td>
                        <td>
                          {row.reset ? (
                            <button
                              type="button"
                              onClick={() => updateRow(index, { reset: false })}
                            >
                              撤销
                            </button>
                          ) : (
                            <button
                              type="button"
                              onClick={() => updateRow(index, { reset: true })}
                            >
                              恢复内置价
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="pricing-actions">
                  <button
                    type="button"
                    onClick={() =>
                      setPriceRows((rows) => [
                        ...rows,
                        { model: '', input: '', output: '', reset: false },
                      ])
                    }
                  >
                    + 新增模型
                  </button>
                  <button
                    type="button"
                    className="pricing-save-btn"
                    disabled={pricingSaving}
                    onClick={() => void savePricing()}
                  >
                    {pricingSaving ? '保存中…' : '保存单价'}
                  </button>
                </div>
              </>
            ) : (
              <p className="usage-empty">展开后加载单价配置…</p>
            )}
          </details>
        </>
      ) : (
        <div className="usage-empty">暂无用量记录，Sprint 运行后自动统计。</div>
      )}
    </details>
  )
}

/** 简单行级 diff：基于 LCS，把两份文本对比为 same/add/del 行序列（内容已裁剪，行数有限） */
type DiffLine = { type: 'same' | 'add' | 'del'; text: string }

function diffLines(oldText: string, newText: string): DiffLine[] {
  const a = oldText.split('\n')
  const b = newText.split('\n')
  const m = a.length
  const n = b.length
  const dp: number[][] = Array.from({ length: m + 1 }, () =>
    new Array<number>(n + 1).fill(0),
  )
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] =
        a[i] === b[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      out.push({ type: 'same', text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ type: 'del', text: a[i] })
      i++
    } else {
      out.push({ type: 'add', text: b[j] })
      j++
    }
  }
  while (i < m) out.push({ type: 'del', text: a[i++] })
  while (j < n) out.push({ type: 'add', text: b[j++] })
  return out
}

/** 版本链面板：展示同一产物的全部历史版本，支持查看内容、相邻版本对比与回滚 */
function VersionChainPanel({
  artifact,
  onClose,
}: {
  artifact: OfficeArtifact
  onClose: () => void
}) {
  const [versions, setVersions] = useState<OfficeArtifact[]>([])
  const [loading, setLoading] = useState(true)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [diffFor, setDiffFor] = useState<string | null>(null)
  const [rolling, setRolling] = useState(false)

  async function load() {
    setLoading(true)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/versions`,
      )
      if (!res.ok) throw new Error(String(res.status))
      const payload = (await res.json()) as { versions?: OfficeArtifact[] }
      setVersions(payload.versions ?? [])
    } catch {
      showToast('加载版本链失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifact.id])

  async function rollback(target: OfficeArtifact) {
    if (rolling) return
    setRolling(true)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/rollback`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target_id: target.id }),
        },
      )
      const payload = (await res.json().catch(() => null)) as
        | { artifact?: OfficeArtifact; detail?: string }
        | null
      if (!res.ok) {
        showToast(payload?.detail ?? '回滚失败')
        return
      }
      if (payload?.artifact) upsertArtifact(payload.artifact)
      showToast(`已按 v${target.version ?? 1} 的内容生成回滚版本，旧版仍保留`)
      await load()
    } catch {
      showToast('回滚失败，请确认后端已启动')
    } finally {
      setRolling(false)
    }
  }

  const latestId = versions[versions.length - 1]?.id

  return (
    <div className="version-chain-panel">
      <header>
        <strong>版本链（{versions.length} 个版本）</strong>
        <button type="button" onClick={onClose}>
          收起
        </button>
      </header>
      {loading ? <p className="view-card-meta">加载中…</p> : null}
      {!loading &&
        versions.map((v, index) => {
          const prev = index > 0 ? versions[index - 1] : null
          const showDiff = diffFor === v.id && prev
          return (
            <div
              key={v.id}
              className={`version-row ${v.id === latestId ? 'latest' : ''}`}
            >
              <div className="version-row-head">
                <span className="artifact-file-badge">v{v.version ?? 1}</span>
                <strong>{v.title}</strong>
                <em>{timeLabel(v.created_at)}</em>
                {v.id === latestId ? (
                  <span className="version-latest-badge">当前最新</span>
                ) : null}
              </div>
              <div className="version-row-actions">
                <button
                  type="button"
                  onClick={() =>
                    setExpandedId(expandedId === v.id ? null : v.id)
                  }
                >
                  {expandedId === v.id ? '收起内容' : '查看内容'}
                </button>
                {prev ? (
                  <button
                    type="button"
                    onClick={() => setDiffFor(showDiff ? null : v.id)}
                  >
                    {showDiff ? '收起对比' : `对比 v${prev.version ?? index}`}
                  </button>
                ) : null}
                {v.id !== latestId ? (
                  <button
                    type="button"
                    disabled={rolling}
                    onClick={() => void rollback(v)}
                  >
                    {rolling ? '回滚中…' : '回滚到此版'}
                  </button>
                ) : null}
              </div>
              {expandedId === v.id ? (
                <pre className="view-md version-content">{v.content}</pre>
              ) : null}
              {showDiff && prev ? (
                <pre className="version-diff">
                  {diffLines(prev.content, v.content).map((line, i) => (
                    <span key={i} className={`diff-${line.type}`}>
                      {line.type === 'add' ? '+ ' : line.type === 'del' ? '- ' : '  '}
                      {line.text}
                      {'\n'}
                    </span>
                  ))}
                </pre>
              ) : null}
            </div>
          )
        })}
    </div>
  )
}

/** 产物内容编辑弹窗：载入原始全文，保存为版本链上的新「编辑 vN」版本 */
function ArtifactEditModal({
  artifact,
  onClose,
}: {
  artifact: OfficeArtifact
  onClose: () => void
}) {
  const [content, setContent] = useState('')
  const [truncated, setTruncated] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await fetch(
          `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/raw`,
        )
        if (!res.ok) throw new Error(String(res.status))
        const payload = (await res.json()) as {
          content: string
          truncated: boolean
        }
        if (cancelled) return
        setContent(payload.content)
        setTruncated(Boolean(payload.truncated))
      } catch {
        if (!cancelled) showToast('加载产物内容失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [artifact.id])

  async function save() {
    if (saving) return
    setSaving(true)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/edit`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content }),
        },
      )
      const payload = (await res.json().catch(() => null)) as {
        detail?: string
      } | null
      if (!res.ok) {
        showToast(payload?.detail ?? '保存失败')
        return
      }
      showToast('已保存为新版本', { kind: 'success' })
      onClose()
    } catch {
      showToast('保存失败，请确认后端已启动')
    } finally {
      setSaving(false)
    }
  }

  const isDocx = Boolean(artifact.file_name?.toLowerCase().match(/\.(docx|pdf)$/))

  return createPortal(
    <div className="sprint-backdrop" onClick={onClose}>
      <div
        className="sprint-dialog artifact-edit-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="sprint-dialog-head">
          <h3>编辑：{artifact.title}</h3>
          <button
            type="button"
            className="sprint-dialog-close"
            aria-label="关闭"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        {truncated ? (
          <p className="artifact-edit-warning">
            ⚠ 内容超200KB被截断，保存将截断版本
          </p>
        ) : null}
        {isDocx ? (
          <p className="sprint-dialog-tip">
            保存后将按原格式重新生成 Word 或 PDF 文件（支持 Markdown 标题和列表）
          </p>
        ) : null}
        {loading ? (
          <p className="view-card-meta">内容加载中…</p>
        ) : (
          <textarea
            className="artifact-edit-textarea"
            value={content}
            onChange={(event) => setContent(event.target.value)}
          />
        )}
        <div className="sprint-dialog-actions">
          <button type="button" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary"
            disabled={loading || saving}
            onClick={() => void save()}
          >
            {saving ? '保存中…' : '保存为新版本'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

/** HTML 产物沙箱预览：iframe 仅允许脚本，隔离文档域 */
function ArtifactSandboxModal({
  artifact,
  onClose,
}: {
  artifact: OfficeArtifact
  onClose: () => void
}) {
  return createPortal(
    <div className="sprint-backdrop" onClick={onClose}>
      <div
        className="sprint-dialog artifact-sandbox-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="sprint-dialog-head">
          <h3>沙箱预览：{artifact.title}</h3>
          <button
            type="button"
            className="sprint-dialog-close"
            aria-label="关闭"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <iframe
          className="artifact-sandbox-iframe"
          sandbox="allow-scripts"
          title={`沙箱预览：${artifact.title}`}
          src={`${OFFICE_API_BASE}/api/artifacts/${artifact.id}/code_preview`}
        />
      </div>
    </div>,
    document.body,
  )
}

function ArtifactsView() {
  const [error, setError] = useState<string | null>(null)
  const [loadingAll, setLoadingAll] = useState(true)
  const [allArtifacts, setAllArtifacts] = useState<OfficeArtifact[]>([])
  const [artifactQuery, setArtifactQuery] = useState('')
  const [openId, setOpenId] = useState<string | null>(null)
  const [qualityFor, setQualityFor] = useState<string | null>(null)
  const [editFor, setEditFor] = useState<OfficeArtifact | null>(null)
  const [sandboxFor, setSandboxFor] = useState<OfficeArtifact | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [deletingGroup, setDeletingGroup] = useState<string | null>(null)
  const [reviseFor, setReviseFor] = useState<string | null>(null)
  const [reviseText, setReviseText] = useState('')
  const [versionsFor, setVersionsFor] = useState<string | null>(null)
  const currentArtifacts = useSyncExternalStore(subscribeArtifacts, getArtifactsSnapshot)

  async function refreshDeliverables() {
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/artifacts/deliverables`)
      if (!res.ok) throw new Error(String(res.status))
      const payload = (await res.json()) as {
        artifacts?: Array<OfficeArtifact & { sprint_requirement?: string; sprint_started_at?: number }>
      }
      setAllArtifacts(payload.artifacts ?? [])
      setError(null)
      return true
    } catch {
      setError('无法加载历史产物，请确认后端已启动')
      return false
    } finally {
      setLoadingAll(false)
    }
  }

  useEffect(() => {
    void refreshDeliverables()
    // 首次挂载读取跨 Sprint 的最终交付物。
  }, [])

  useEffect(() => {
    if (currentArtifacts.length > 0) void refreshDeliverables()
    // WebSocket updates the current Sprint artifact store when a final file is generated.
  }, [currentArtifacts])

  const mine = allArtifacts.filter((artifact) => artifact.kind === 'final' || artifact.kind === 'custom')
  const grouped = new Map<string, { id: string; date: string; requirement: string; items: OfficeArtifact[] }>()
  for (const artifact of mine) {
    const item = artifact as OfficeArtifact & { sprint_requirement?: string; sprint_started_at?: number }
    const timestamp = item.sprint_started_at || artifact.created_at
    const date = timestamp ? new Date(timestamp * 1000).toLocaleDateString('zh-CN') : '日期未知'
    const requirement = item.sprint_requirement?.trim() || '独立生成'
    const key = artifact.sprint_id || artifact.id
    const group = grouped.get(key) ?? { id: key, date, requirement, items: [] }
    group.items.push(artifact)
    grouped.set(key, group)
  }
  const query = artifactQuery.trim().toLocaleLowerCase()
  const groups = [...grouped.values()].filter((group) =>
    !query || group.requirement.toLocaleLowerCase().includes(query) ||
    group.items.some((artifact) => artifact.title.toLocaleLowerCase().includes(query)),
  )
  for (const group of groups) {
    group.items.sort((a, b) => a.title.localeCompare(b.title, 'zh-CN') || b.created_at - a.created_at)
  }

  function download(artifact: OfficeArtifact) {
    downloadArtifact(artifact)
  }

  async function copy(artifact: OfficeArtifact) {
    try {
      await navigator.clipboard.writeText(artifact.content)
      showToast('全文已复制到剪贴板')
    } catch {
      showToast('复制失败，请手动选择文本复制')
    }
  }

  async function retry(artifact: OfficeArtifact) {
    if (busyId) return
    setBusyId(artifact.id)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/retry`,
        { method: 'POST' },
      )
      const payload = (await res.json().catch(() => null)) as
        | { artifact?: OfficeArtifact; detail?: string }
        | null
      if (!res.ok) {
        showToast(payload?.detail ?? '重新生成失败')
        return
      }
      if (payload?.artifact) upsertArtifact(payload.artifact)
      await refreshDeliverables()
      showToast('已重新生成')
    } catch {
      showToast('重新生成失败，请确认后端已启动')
    } finally {
      setBusyId(null)
    }
  }

  async function revise(artifact: OfficeArtifact) {
    const instruction = reviseText.trim()
    if (!instruction || busyId) return
    setBusyId(artifact.id)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/revise`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ instruction }),
        },
      )
      const payload = (await res.json().catch(() => null)) as
        | { artifact?: OfficeArtifact; detail?: string }
        | null
      if (!res.ok) {
        showToast(payload?.detail ?? '生成修订版失败')
        return
      }
      if (payload?.artifact) {
        upsertArtifact(payload.artifact)
        setAllArtifacts((items) => [...items.filter((item) => item.id !== payload.artifact!.id), payload.artifact!])
        setOpenId(payload.artifact.id)
      }
      setReviseFor(null)
      setReviseText('')
      showToast('已生成修订版，原版本仍保留可追溯')
    } catch {
      showToast('生成修订版失败，请确认后端已启动')
    } finally {
      setBusyId(null)
    }
  }

  async function reveal(artifact: OfficeArtifact) {
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/reveal`,
        { method: 'POST' },
      )
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        showToast(typeof detail?.detail === 'string' ? detail.detail : '打开失败')
      }
    } catch {
      showToast('打开所在文件夹失败，请确认后端已启动')
    }
  }

  /** 结构化预览：Word/PPT/Excel 在后端解析为 HTML 新标签页展示 */
  function preview(artifact: OfficeArtifact) {
    window.open(
      `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/preview`,
      '_blank',
      'noopener',
    )
  }

  async function removeOne(artifact: OfficeArtifact) {
    const ok = window.confirm(
      `确定删除产物「${artifact.title}」？其磁盘文件将一并删除，不可恢复。`,
    )
    if (!ok) return
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}`,
        { method: 'DELETE' },
      )
      const detail = (await res.json().catch(() => null)) as {
        detail?: string
      } | null
      if (!res.ok) {
        showToast(detail?.detail ?? '删除失败')
        return
      }
      removeArtifact(artifact.id)
      setAllArtifacts((items) => items.filter((item) => item.id !== artifact.id))
      showToast('产物已删除')
    } catch {
      showToast('删除失败，请确认后端已启动')
    }
  }

  async function removeGroup(group: { id: string; requirement: string; items: OfficeArtifact[] }) {
    if (deletingGroup) return
    if (!window.confirm(`确定删除任务「${group.requirement}」的所有交付产物？对应磁盘文件也会删除，且不可恢复。`)) return
    setDeletingGroup(group.id)
    const deletedIds = new Set<string>()
    let failed = false
    try {
      for (const artifact of group.items) {
        const res = await fetch(`${OFFICE_API_BASE}/api/artifacts/${artifact.id}`, { method: 'DELETE' })
        if (!res.ok) { failed = true; break }
        removeArtifact(artifact.id)
        deletedIds.add(artifact.id)
      }
    } catch {
      failed = true
    } finally {
      setAllArtifacts((items) => items.filter((item) => !deletedIds.has(item.id)))
      setDeletingGroup(null)
      showToast(failed ? '部分交付删除失败，未删除的仍保留在该任务下' : '该任务的交付产物已删除')
    }
  }

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>最终交付</h1>
          <p>按日期和需求名称归档成品；开发、测试、评审及复盘记录可在「过程文件」查看。</p>
        </div>
      </header>

      <label className="artifact-history-search">
        <span>搜索产物</span>
        <input
          value={artifactQuery}
          onChange={(event) => setArtifactQuery(event.target.value)}
          placeholder="按产物名称或需求名称搜索"
        />
      </label>

      <UsagePanel />

      {error ? <div className="view-empty">{error}</div> : null}

      {!error ? (
        loadingAll ? (
          <div className="view-empty">正在加载历史产物…</div>
        ) : groups.length === 0 ? (
          <div className="view-empty">
            {artifactQuery ? '没有匹配的最终产物。' : '暂无最终产物。完成 Sprint 后，成品会归档在这里。'}
          </div>
        ) : (
          <div className="artifact-history-groups">
            {groups.map((group) => (
              <div className="artifact-history-group" key={group.id}>
              <details className="artifact-history-entry">
                <summary>
                  <time>{group.date}</time>
                  <h2>{group.requirement}</h2>
                  <span className="artifact-history-chevron" aria-hidden="true" />
                </summary>
                <div className="artifact-doc-list">
            {group.items.map((artifact) => {
              const open = openId === artifact.id
              const degraded = Boolean(artifact.degraded)
              const previewable = canPreviewFile(artifact)
              const legacyDemo = (artifact as OfficeArtifact & { sprint_requirement?: string }).sprint_requirement?.startsWith('【演示模式】') ?? false
              return (
                <article
                  key={artifact.id}
                  className={`artifact-doc ${degraded ? 'artifact-degraded' : ''}`}
                >
                  <div className="artifact-doc-head">
                    <button
                      type="button"
                      className="artifact-doc-toggle"
                      aria-expanded={open}
                      onClick={() => setOpenId(open ? null : artifact.id)}
                    >
                      <strong>{artifact.title}</strong>
                      <span className="artifact-doc-badges">
                        <span className="artifact-source-badge">{sourceLabel(artifact, legacyDemo)}</span>
                        <span className={`artifact-quality-badge ${artifact.quality_status === 'failed' ? 'is-failed' : ''}`}>{qualityLabel(artifact)}</span>
                        <span className="artifact-file-badge">{artifact.file_name ? `文件可下载 ${fileExt(artifact) ?? ''}` : '仅文字摘要'}</span>
                        {artifact.version && artifact.version > 1 ? <span className="artifact-file-badge">v{artifact.version}</span> : null}
                      </span>
                      <em>{timeLabel(artifact.created_at)}</em>
                    </button>
                    <div className="artifact-doc-quick-actions">
                      {artifact.file_name || artifact.kind === 'final' ? <button type="button" aria-expanded={qualityFor === artifact.id} onClick={() => setQualityFor(qualityFor === artifact.id ? null : artifact.id)}>验收详情</button> : null}
                      {previewable ? (
                        artifact.file_name?.toLowerCase().endsWith('.html') ? (
                          <button type="button" onClick={() => setSandboxFor(artifact)}>预览</button>
                        ) : (
                          <button type="button" onClick={() => preview(artifact)}>预览</button>
                        )
                      ) : null}
                      {!artifact.file_name ? <button type="button" onClick={() => setOpenId(open ? null : artifact.id)}>查看摘要</button> : null}
                      <button type="button" className="artifact-quick-download" onClick={() => download(artifact)}>{artifact.file_name ? '下载文件' : '下载摘要'}</button>
                      {artifact.file_name ? <button type="button" onClick={() => void reveal(artifact)}>打开所在文件夹</button> : null}
                      <button type="button" className="artifact-revise-btn" onClick={() => { setOpenId(artifact.id); setReviseFor(artifact.id); setReviseText('') }}>继续修改</button>
                      <button type="button" className="artifact-delete-btn" onClick={() => void removeOne(artifact)}>删除</button>
                    </div>
                  </div>
                  {qualityFor === artifact.id ? <ArtifactQualityPanel artifactId={artifact.id} /> : null}
                  {open ? (
                    <>
                      {degraded && artifact.degraded_reason ? (
                        <p className="artifact-degraded-tip">
                          ⚠ 内容或文件存在问题：{artifact.degraded_reason}
                        </p>
                      ) : null}
                      <pre className="view-md artifact-doc-body">
                        {artifact.content}
                      </pre>
                      {reviseFor === artifact.id ? (
                        <div className="artifact-revise-row">
                          <input
                            autoFocus
                            value={reviseText}
                            placeholder="说明怎么改，例如：第二章再展开一点，补三个案例"
                            onChange={(event) => setReviseText(event.target.value)}
                            onKeyDown={(event) => {
                              if (event.key === 'Enter') void revise(artifact)
                            }}
                          />
                          <button
                            type="button"
                            disabled={busyId === artifact.id || !reviseText.trim()}
                            onClick={() => void revise(artifact)}
                          >
                            {busyId === artifact.id ? '生成中…' : '生成修订版'}
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              setReviseFor(null)
                              setReviseText('')
                            }}
                          >
                            取消
                          </button>
                        </div>
                      ) : null}
                      {versionsFor === artifact.id ? (
                        <VersionChainPanel
                          artifact={artifact}
                          onClose={() => setVersionsFor(null)}
                        />
                      ) : null}
                      <footer>
                        <button type="button" onClick={() => copy(artifact)}>
                          复制全文
                        </button>
                        <button
                          type="button"
                          className="artifact-edit-btn"
                          onClick={() => setEditFor(artifact)}
                        >
                          ✏️ 编辑
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            setVersionsFor(
                              versionsFor === artifact.id ? null : artifact.id,
                            )
                          }
                        >
                          {versionsFor === artifact.id ? '收起版本' : '版本'}
                        </button>
                        <button
                          type="button"
                          className="artifact-retry-btn"
                          disabled={busyId === artifact.id}
                          onClick={() => void retry(artifact)}
                        >
                          {busyId === artifact.id ? '重试中…' : '重新生成'}
                        </button>
                        <button
                          type="button"
                          className="artifact-revise-btn"
                          onClick={() => {
                            setReviseFor(
                              reviseFor === artifact.id ? null : artifact.id,
                            )
                            setReviseText('')
                          }}
                        >
                          继续修改
                        </button>
                        <button
                          type="button"
                          className="artifact-delete-btn"
                          onClick={() => void removeOne(artifact)}
                        >
                          删除
                        </button>
                      </footer>
                    </>
                  ) : null}
                </article>
              )
            })}
                </div>
              </details>
              <button type="button" className="artifact-history-delete artifact-delete-btn" disabled={deletingGroup !== null} aria-label={`删除任务「${group.requirement}」的所有交付产物`} onClick={() => void removeGroup(group)}>{deletingGroup === group.id ? '删除中…' : '删除'}</button>
              </div>
            ))}
          </div>
        )
      ) : null}
      {editFor ? (
        <ArtifactEditModal artifact={editFor} onClose={() => { setEditFor(null); void refreshDeliverables() }} />
      ) : null}
      {sandboxFor ? (
        <ArtifactSandboxModal
          artifact={sandboxFor}
          onClose={() => setSandboxFor(null)}
        />
      ) : null}
    </div>
  )
}

/* ------------------------------ 视图切换器 ------------------------------ */

export function OfficeViewPanel({ view }: { view: OfficeView }) {
  const tabs: { label: string; view: OfficeView }[] =
    view === 'tasks' || view === 'calendar'
      ? [{ label: '任务列表', view: 'tasks' }, { label: '里程碑与动态', view: 'calendar' }]
      : view === 'artifacts' || view === 'files' || view === 'knowledge'
        ? [{ label: '最终交付', view: 'artifacts' }, { label: '过程文件', view: 'files' }, { label: '参考资料', view: 'knowledge' }]
        : view === 'agents' || view === 'skills'
          ? [{ label: '成员', view: 'agents' }, { label: '技能', view: 'skills' }]
          : []
  let content: ReactNode
  switch (view) {
    case 'tasks':
      content = <TaskCenterView />
      break
    case 'agents':
      content = <AgentsView />
      break
    case 'files':
      content = <FilesView />
      break
    case 'knowledge':
      content = <KnowledgeView />
      break
    case 'artifacts':
      content = <ArtifactsView />
      break
    case 'skills':
      content = <SkillsView />
      break
    case 'browser':
      return <BrowserView />
    case 'chat':
      return <OfficeChatView />
    case 'calendar':
      content = <CalendarView />
      break
    case 'notifications':
      return <NotificationsView />
    default:
      return null
  }
  return (
    <div className="view-with-tabs">
      <nav className="section-tabs" aria-label="页面内容">
        {tabs.map((tab) => (
          <button key={tab.view} type="button" className={view === tab.view ? 'active' : ''} aria-current={view === tab.view ? 'page' : undefined} onClick={() => setActiveView(tab.view)}>
            {tab.label}
          </button>
        ))}
      </nav>
      {content}
    </div>
  )
}
