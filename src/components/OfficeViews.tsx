import { lazy, type ReactNode } from 'react'
import { setActiveView, type OfficeView } from '@/store/officeViewStore'
const AgentsView = lazy(() => import('./views/AgentsView').then(module => ({ default: module.AgentsView })))
const ArtifactsView = lazy(() => import('./views/ArtifactsView').then(module => ({ default: module.ArtifactsView })))
const BrowserView = lazy(() => import('./views/BrowserView').then(module => ({ default: module.BrowserView })))
const CalendarView = lazy(() => import('./views/CalendarView').then(module => ({ default: module.CalendarView })))
const FilesView = lazy(() => import('./views/FilesView').then(module => ({ default: module.FilesView })))
const KnowledgeView = lazy(() => import('./views/KnowledgeView').then(module => ({ default: module.KnowledgeView })))
const NotificationsView = lazy(() => import('./views/NotificationsView').then(module => ({ default: module.NotificationsView })))
const SkillsView = lazy(() => import('./views/SkillsView').then(module => ({ default: module.SkillsView })))
const TalkView = lazy(() => import('./views/TalkView').then(module => ({ default: module.TalkView })))
const TaskCenterView = lazy(() => import('./views/TaskCenterView').then(module => ({ default: module.TaskCenterView })))
const OfficeChatView = lazy(() => import('./OfficeChatView').then(module => ({ default: module.OfficeChatView })))

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
    case 'talk':
      return <TalkView />
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
