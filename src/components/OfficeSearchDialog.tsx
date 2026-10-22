import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'
import { setSelectedRoster } from '@/store/officeChatStore'
import {
  getActiveView,
  isSearchOpen,
  setActiveView,
  setSearchOpen,
  setViewedSprint,
  subscribeView,
} from '@/store/officeViewStore'

type SearchPayload = {
  sprints: { id: string; requirement: string; status: string; started_at: number }[]
  tasks: { sprint_id: string; id: string; title: string; status: string }[]
  artifacts: {
    id: string
    sprint_id: string
    title: string
    kind: string
    file_name: string | null
    created_at: number
  }[]
}

const EMPTY_PAYLOAD: SearchPayload = { sprints: [], tasks: [], artifacts: [] }

/** 全局搜索：Ctrl/⌘ + K 呼出，覆盖历史需求、任务、产物/文件名与员工并跳转 */
export function OfficeSearchDialog() {
  const open = useSyncExternalStore(subscribeView, isSearchOpen)
  const activeView = useSyncExternalStore(subscribeView, getActiveView)
  const [query, setQuery] = useState('')
  const [remote, setRemote] = useState<SearchPayload>(EMPTY_PAYLOAD)
  const [searching, setSearching] = useState(false)
  // 防抖：输入停止 250ms 后再请求后端，避免每个字符都打一次查询
  const debounceRef = useRef<number | undefined>(undefined)

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setSearchOpen(!isSearchOpen())
      }
      if (event.key === 'Escape') setSearchOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    if (!open) {
      setQuery('')
      setRemote(EMPTY_PAYLOAD)
    }
  }, [open])

  useEffect(() => {
    const keyword = query.trim()
    window.clearTimeout(debounceRef.current)
    if (!keyword) {
      setRemote(EMPTY_PAYLOAD)
      setSearching(false)
      return
    }
    setSearching(true)
    debounceRef.current = window.setTimeout(async () => {
      try {
        const res = await fetch(
          `${OFFICE_API_BASE}/api/search?q=${encodeURIComponent(keyword)}`,
        )
        if (!res.ok) throw new Error(String(res.status))
        setRemote((await res.json()) as SearchPayload)
      } catch {
        setRemote(EMPTY_PAYLOAD)
      } finally {
        setSearching(false)
      }
    }, 250)
    return () => window.clearTimeout(debounceRef.current)
  }, [query])

  if (!open) return null

  const q = query.trim().toLowerCase()
  const agents = q
    ? AGENT_ROSTER.map((entry, index) => ({ entry, roster: index + 1 })).filter(
        ({ entry }) =>
          `${entry.name}${entry.role}${entry.skills.join('')}`
            .toLowerCase()
            .includes(q),
      )
    : []
  const { sprints, tasks, artifacts } = remote
  const hasResults =
    agents.length > 0 || sprints.length > 0 || tasks.length > 0 || artifacts.length > 0

  function jumpToAgent(roster: number) {
    setSelectedRoster(roster)
    setActiveView('chat')
    setSearchOpen(false)
  }

  /** 历史需求：全局切换器切到该 Sprint 并打开产物中心 */
  function jumpToSprint(sprintId: string) {
    void setViewedSprint(sprintId)
    setActiveView('artifacts')
    setSearchOpen(false)
  }

  function jumpToTask(sprintId: string) {
    void setViewedSprint(sprintId)
    setActiveView('tasks')
    setSearchOpen(false)
  }

  function jumpToArtifact(sprintId: string) {
    void setViewedSprint(sprintId)
    setActiveView('artifacts')
    setSearchOpen(false)
  }

  return (
    <div className="sprint-backdrop search-backdrop" onClick={() => setSearchOpen(false)}>
      <div
        className="search-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="search-input-row">
          <span>⌕</span>
          <input
            autoFocus
            value={query}
            placeholder="搜索历史需求、任务、产物/文件名、员工…"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                if (agents.length > 0) jumpToAgent(agents[0]!.roster)
                else if (sprints.length > 0) jumpToSprint(sprints[0]!.id)
                else if (artifacts.length > 0) jumpToArtifact(artifacts[0]!.sprint_id)
                else if (tasks.length > 0) jumpToTask(tasks[0]!.sprint_id)
              }
            }}
          />
          <kbd>Esc</kbd>
        </div>

        <div className="search-results">
          {!q ? (
            <>
              <p className="search-hint">快速跳转</p>
              {(
                [
                  ['任务中心', 'tasks'],
                  ['智能体', 'agents'],
                  ['产物中心', 'artifacts'],
                  ['文件中心', 'files'],
                  ['对话中心', 'chat'],
                  ['日程日历', 'calendar'],
                  ['通知中心', 'notifications'],
                ] as const
              ).map(([label, view]) => (
                <button
                  key={view}
                  type="button"
                  className="search-result"
                  onClick={() => {
                    setActiveView(view)
                    setSearchOpen(false)
                  }}
                >
                  <strong>{label}</strong>
                  <em>{activeView === view ? '当前页面' : '跳转'}</em>
                </button>
              ))}
            </>
          ) : hasResults ? (
            <>
              {agents.length > 0 ? (
                <>
                  <p className="search-hint">员工（{agents.length}）</p>
                  {agents.map(({ entry, roster }) => (
                    <button
                      key={entry.id}
                      type="button"
                      className="search-result"
                      onClick={() => jumpToAgent(roster)}
                    >
                      <strong>{entry.name} · {entry.role}</strong>
                      <em>去对话</em>
                    </button>
                  ))}
                </>
              ) : null}
              {sprints.length > 0 ? (
                <>
                  <p className="search-hint">历史需求（{sprints.length}）</p>
                  {sprints.map((sprint) => (
                    <button
                      key={sprint.id}
                      type="button"
                      className="search-result"
                      onClick={() => jumpToSprint(sprint.id)}
                    >
                      <strong>{sprint.requirement.slice(0, 40) || sprint.id}</strong>
                      <em>
                        {new Date(sprint.started_at * 1000).toLocaleDateString()} ·{' '}
                        {sprint.status}
                      </em>
                    </button>
                  ))}
                </>
              ) : null}
              {artifacts.length > 0 ? (
                <>
                  <p className="search-hint">产物（{artifacts.length}）</p>
                  {artifacts.map((artifact) => (
                    <button
                      key={artifact.id}
                      type="button"
                      className="search-result"
                      onClick={() => jumpToArtifact(artifact.sprint_id)}
                    >
                      <strong>{artifact.title}</strong>
                      <em>{artifact.file_name ?? artifact.kind}</em>
                    </button>
                  ))}
                </>
              ) : null}
              {tasks.length > 0 ? (
                <>
                  <p className="search-hint">任务（{tasks.length}）</p>
                  {tasks.map((task) => (
                    <button
                      key={`${task.sprint_id}-${task.id}`}
                      type="button"
                      className="search-result"
                      onClick={() => jumpToTask(task.sprint_id)}
                    >
                      <strong>{task.title || task.id}</strong>
                      <em>{task.status}</em>
                    </button>
                  ))}
                </>
              ) : null}
            </>
          ) : (
            <p className="search-hint">
              {searching ? '搜索中…' : '没有匹配的需求、任务、产物或员工'}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
