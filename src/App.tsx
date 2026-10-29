import { lazy, Suspense, useEffect, useSyncExternalStore } from 'react'
import { OfficeActionConnector } from '@/components/OfficeActionConnector'
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
import { primeAudio } from '@/services/officeSound'
import './App.css'

const OfficeCanvas = lazy(() => import('@/components/OfficeCanvas').then(module => ({ default: module.OfficeCanvas })))

function App() {
  const view = useSyncExternalStore(subscribeView, getActiveView)

  // 启动即拉取成员配置：自定义姓名要同步到场景名牌/名册，不管用户先打开哪个页面
  useEffect(() => {
    initTheme()
    ensureAgentConfigsLoaded()
    // 首次用户手势预热音频上下文，避免 Sprint 结束提示音被浏览器自动播放策略拦截
    let primed = false
    const prime = () => {
      if (primed) return
      primed = true
      primeAudio()
      window.removeEventListener('pointerdown', prime)
      window.removeEventListener('keydown', prime)
    }
    window.addEventListener('pointerdown', prime)
    window.addEventListener('keydown', prime)
    return () => {
      window.removeEventListener('pointerdown', prime)
      window.removeEventListener('keydown', prime)
    }
  }, [])

  return (
    <div className="office-app">
      <OfficeActionConnector />
      <OfficeSidebar />
      <div className="office-center">
        <OfficeHeaderStats />
        <main className="office-main">
          <Suspense fallback={<div role="status" aria-live="polite">正在加载…</div>}>
            {view === 'home' ? <OfficeCanvas /> : <OfficeViewPanel view={view} />}
          </Suspense>
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
