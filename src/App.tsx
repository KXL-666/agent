import { useEffect, useSyncExternalStore } from 'react'
import { OfficeActionConnector } from '@/components/OfficeActionConnector'
import { OfficeCanvas } from '@/components/OfficeCanvas'
import {
  OfficeBottomToolbar,
  OfficeHeaderStats,
  OfficeRightPanel,
  OfficeSidebar,
  OfficeToast,
} from '@/components/OfficeDashboardChrome'
import { OfficeSearchDialog } from '@/components/OfficeSearchDialog'
import { OfficeViewPanel } from '@/components/OfficeViews'
import { ensureAgentConfigsLoaded } from '@/store/officeAgentConfigStore'
import { initTheme } from '@/store/officeThemeStore'
import { getActiveView, subscribeView } from '@/store/officeViewStore'
import './App.css'

function App() {
  const view = useSyncExternalStore(subscribeView, getActiveView)

  // 启动即拉取成员配置：自定义姓名要同步到场景名牌/名册，不管用户先打开哪个页面
  useEffect(() => {
    initTheme()
    ensureAgentConfigsLoaded()
  }, [])

  return (
    <div className="office-app">
      <OfficeActionConnector />
      <OfficeSidebar />
      <div className="office-center">
        <OfficeHeaderStats />
        <main className="office-main">
          {view === 'home' ? <OfficeCanvas /> : <OfficeViewPanel view={view} />}
          <OfficeBottomToolbar />
        </main>
      </div>
      <OfficeRightPanel />
      <OfficeSearchDialog />
      <OfficeToast />
    </div>
  )
}

export default App
