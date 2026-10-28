import { OFFICE_API_BASE } from '@/config/officeMode'
import { captureOfficeScreenshot } from '@/scene/officeSceneBridge'
import { showToast } from '@/store/officeUiStore'

export function BrowserView() {
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
