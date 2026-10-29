import { useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { SprintSettingsButton } from './sprintSettings'
import {
  getUnreadSnapshot,
  subscribeNotifications,
} from '@/store/officeEventStore'
import {
  getTasksSnapshot,
  isSprintRunning,
  subscribeSprintData,
} from '@/store/officeTaskStore'
import {
  clearArtifactPulse,
  getArtifactPulseAt,
  subscribeToast,
} from '@/store/officeUiStore'
import {
  createConversation,
  deleteConversation,
  getSidebarConversations,
  openConversation,
  setSelectedRoster,
  subscribeChat,
} from '@/store/officeChatStore'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'
import {
  getAgentConfigsSnapshot,
  subscribeAgentConfigs,
} from '@/store/officeAgentConfigStore'
import {
  getActiveView,
  getViewedSprint,
  setActiveView,
  setSearchOpen,
  subscribeView,
  type OfficeView,
} from '@/store/officeViewStore'

const NAV_ITEMS: { label: string; view: OfficeView; hint: string }[] = [
  { label: '对话', view: 'talk', hint: '任务指令输入：描述需求、下达指示与快捷操作' },
  { label: '办公室', view: 'home', hint: '办公室全景与实时动态' },
  { label: '任务', view: 'tasks', hint: '任务看板：进度、负责人与任务详情' },
  { label: '交付', view: 'artifacts', hint: '最终交付物、过程文件与验收结果' },
  { label: '团队', view: 'agents', hint: '六位成员的名册、API 配置与技能' },
  { label: '单飞', view: 'chat', hint: '与团队对话，可直接发起新任务' },
]

const NAV_GROUPS: Record<string, OfficeView[]> = {
  talk: ['talk'],
  home: ['home'],
  tasks: ['tasks', 'calendar'],
  artifacts: ['artifacts', 'files', 'knowledge'],
  agents: ['agents', 'skills'],
  chat: ['chat'],
}

function SidebarIcon({ view }: { view: OfficeView }) {
  const paths: Record<string, ReactNode> = {
    home: <><path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z" /><path d="M9 21v-7h6v7" /></>,
    talk: <path d="M4 5h16v12H9l-5 4z" />,
    tasks: <><rect x="3" y="4" width="18" height="17" rx="2" /><path d="m7 9 1.5 1.5L11 8M13 10h5M7 15l1.5 1.5L11 14M13 16h5" /></>,
    artifacts: <><path d="M4 7h16v13H4zM8 7V4h8v3" /><path d="M8 12h8M8 16h5" /></>,
    agents: <><circle cx="9" cy="8" r="3" /><path d="M3 20v-2a6 6 0 0 1 12 0v2M17 6a3 3 0 0 1 0 6M17 15a5 5 0 0 1 4 5" /></>,
    chat: <><path d="m22 2-7 20-4-9-9-4Z" /><path d="M22 2 11 13" /></>,
    notifications: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 8-3 9h18c0-1-3-2-3-9M10 21h4" /></>,
  }
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[view]}</svg>
}

export function OfficeSidebar() {
  const tasks = useSyncExternalStore(subscribeSprintData, getTasksSnapshot)
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const unread = useSyncExternalStore(subscribeNotifications, getUnreadSnapshot)
  const activeView = useSyncExternalStore(subscribeView, getActiveView)
  const pulseAt = useSyncExternalStore(subscribeToast, getArtifactPulseAt)
  // 历史对话列表：团队对话 + 六位成员的单飞对话
  const sidebarConvos = useSyncExternalStore(subscribeChat, getSidebarConversations)
  const agentConfigs = useSyncExternalStore(subscribeAgentConfigs, getAgentConfigsSnapshot)
  const displayNameOf = (rosterNo: number) =>
    agentConfigs[rosterNo]?.name || AGENT_ROSTER[rosterNo - 1]?.name || `#${rosterNo}`
  const importInputRef = useRef<HTMLInputElement | null>(null)
  const [importing, setImporting] = useState(false)
  // 产物落地后「交付」闪一下「新」徽标；6 秒自动熄灭，点开「交付」立即熄灭
  const showNewArtifacts = pulseAt > 0 && activeView !== 'artifacts'

  /** 导入备份 zip：成功后需重启后端并刷新页面才能看到恢复的数据 */
  async function importBackup(file: File) {
    if (importing) return
    setImporting(true)
    try {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch(`${OFFICE_API_BASE}/api/backup/import`, {
        method: 'POST',
        body: form,
      })
      const payload = (await res.json().catch(() => null)) as {
        note?: string
        detail?: string
      } | null
      if (res.status === 409) {
        window.alert(payload?.detail ?? '已有 Sprint 进行中，请先结束 Sprint 再导入备份')
        return
      }
      if (!res.ok) {
        window.alert(payload?.detail ?? '导入备份失败')
        return
      }
      window.alert(payload?.note ?? '备份已导入，请重启后端并刷新页面')
    } catch {
      window.alert('导入备份失败，请确认后端已启动')
    } finally {
      setImporting(false)
      if (importInputRef.current) importInputRef.current.value = ''
    }
  }

  const navItems = NAV_ITEMS.map((item) => {
    if (item.view === 'tasks') {
      return { ...item, badge: running && !viewedSprint && tasks.length > 0 ? tasks.length : undefined, fresh: false }
    }
    if (item.view === 'artifacts') {
      return { ...item, badge: showNewArtifacts ? '新' : undefined, fresh: showNewArtifacts }
    }
    if (item.view === 'notifications') {
      return { ...item, badge: unread > 0 ? unread : undefined, fresh: false }
    }
    return { ...item, badge: undefined, fresh: false }
  })

  function handleNav(item: (typeof NAV_ITEMS)[number]) {
    // 点开「交付」即视为已看过新产物，提醒立即熄灭
    if (item.view === 'artifacts') clearArtifactPulse()
    setActiveView(item.view)
  }

  function handleOpenConversation(roster: number, convId: string) {
    openConversation(roster, convId)
    if (roster === 0) {
      setActiveView('talk')
    } else {
      setSelectedRoster(roster)
      setActiveView('chat')
    }
  }

  function handleDeleteConversation(roster: number, convId: string, title: string) {
    if (!window.confirm(`删除对话「${title}」？此操作不可恢复`)) return
    deleteConversation(roster, convId)
  }

  function handleNewConversation() {
    createConversation(0)
    setActiveView('talk')
  }

  return (
    <aside className="office-sidebar">
      <div className="sidebar-brand">
        <span className="brand-mark" aria-hidden="true">
          {/* 像素风电脑图标：呼应办公室像素场景，crispEdges 保证边缘锐利 */}
          <svg width="16" height="16" viewBox="0 0 16 16" shapeRendering="crispEdges" fill="currentColor">
            <rect x="2" y="2" width="12" height="9" />
            <rect x="4" y="4" width="8" height="5" fill="#d1533f" />
            <rect x="7" y="11" width="2" height="2" />
            <rect x="5" y="13" width="6" height="1" />
          </svg>
        </span>
        <span className="brand-text">
          <strong translate="no">Agile Dev Team</strong>
          <em>多智能体协作办公室</em>
        </span>
      </div>

      <button
        type="button"
        className="sidebar-search"
        onClick={() => setSearchOpen(true)}
        title="全局搜索：功能、历史任务、产物与员工（⌘K）"
      >
        <span>⌕</span>
        <span>搜索功能、任务、员工…</span>
        <kbd>⌘K</kbd>
      </button>

      <nav className="sidebar-nav">
        {navItems.map((item) => (
          <button
            key={item.label}
            type="button"
            className={`nav-item ${NAV_GROUPS[item.view]?.includes(activeView) ? 'active' : ''}`}
            onClick={() => handleNav(item)}
            title={item.view === 'artifacts' && item.badge ? `${item.hint}（有新产物）` : item.hint}
            aria-label={`${item.label}：${item.hint}`}
          >
            <span className="nav-main">
              <span className="nav-icon"><SidebarIcon view={item.view} /></span>
              <span>{item.label}</span>
            </span>
            {item.badge ? (
              <span className={`nav-badge ${item.fresh ? 'is-new' : ''}`}>{item.badge}</span>
            ) : null}
          </button>
        ))}
      </nav>

      <div className="sidebar-convos">
        <div className="sidebar-convos-head">
          <span>历史对话</span>
          <button
            type="button"
            className="sidebar-convos-add"
            onClick={handleNewConversation}
            title="新建对话"
            aria-label="新建对话"
          >
            ＋
          </button>
        </div>
        <ul className="sidebar-convos-list">
          {sidebarConvos.map((item) => (
            <li key={item.conv.id} className="sidebar-convo-row">
              <button
                type="button"
                className={`sidebar-convo ${item.active ? 'active' : ''}`}
                onClick={() => handleOpenConversation(item.roster, item.conv.id)}
                title={`进入对话：${item.conv.title}`}
              >
                <span className="sidebar-convo-title">{item.conv.title}</span>
                {item.roster > 0 ? (
                  <span className="sidebar-convo-agent">{displayNameOf(item.roster)}</span>
                ) : null}
              </button>
              <button
                type="button"
                className="sidebar-convo-del"
                onClick={() => handleDeleteConversation(item.roster, item.conv.id, item.conv.title)}
                title="删除该对话"
                aria-label={`删除对话：${item.conv.title}`}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="sidebar-footer">
        <div className="sidebar-utility-row">
          <button type="button" className={`sidebar-utility ${activeView === 'notifications' ? 'active' : ''}`} onClick={() => setActiveView('notifications')} aria-label={`通知${unread ? `，${unread} 条未读` : ''}`} title="通知中心：Sprint 与任务的状态提醒">
            <SidebarIcon view="notifications" /> 通知 {unread > 0 ? <span className="nav-badge">{unread}</span> : null}
          </button>
          <details className="sidebar-more">
            <summary title="浏览器助手与数据备份/恢复">⋯ 更多工具</summary>
            <button type="button" onClick={() => setActiveView('browser')} title="浏览器助手：打开网页并在站内浏览">浏览器助手</button>
            <details className="sidebar-data">
              <summary title="导出整个工作区备份，或从备份恢复">数据管理</summary>
              <div className="sidebar-backup-row">
                <a href={`${OFFICE_API_BASE}/api/backup/export`} download title="打包下载 office.db 与产物目录">
                  💾 导出备份
                </a>
                <button
                  type="button"
                  disabled={importing}
                  onClick={() => importInputRef.current?.click()}
                  title="导入备份 zip，导入后需重启后端并刷新页面"
                >
                  {importing ? '导入中…' : '📥 导入备份'}
                </button>
              </div>
            </details>
          </details>
          <input
            ref={importInputRef}
            type="file"
            accept=".zip"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) void importBackup(file)
            }}
          />
        </div>
        <div className="user-card">
          <span className="user-avatar">T</span>
          <span>
            <strong>Teemo</strong>
            <em><span className="agent-dot online" />在线</em>
          </span>
        </div>
        <div className="system-status">
          <span className="agent-dot online" />
          <span>本地运行正常</span>
          <SprintSettingsButton />
        </div>
      </div>
    </aside>
  )
}
