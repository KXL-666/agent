import { type ReactNode } from 'react'
import { OfficeChatView } from '@/components/OfficeChatView'
import { setActiveView, type OfficeView } from '@/store/officeViewStore'
import { AgentsView } from './views/AgentsView'
import { ArtifactsView } from './views/ArtifactsView'
import { BrowserView } from './views/BrowserView'
import { CalendarView } from './views/CalendarView'
import { FilesView } from './views/FilesView'
import { KnowledgeView } from './views/KnowledgeView'
import { NotificationsView } from './views/NotificationsView'
import { SkillsView } from './views/SkillsView'
import { TalkView } from './views/TalkView'
import { TaskCenterView } from './views/TaskCenterView'

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
