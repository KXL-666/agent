import { useEffect, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { OFFICE_API_BASE } from '@/config/officeMode'
import {
  getSprintSettingsSnapshot,
  subscribeSprintSettings,
  updateSprintSettings,
} from '@/store/officeSprintSettings'

type LocationItem = { name: string; path: string }

/** 侧栏底部齿轮：任务默认设置（原新建弹窗「更多设置」项），对话页直接发送时生效 */
export function SprintSettingsButton() {
  const settings = useSyncExternalStore(subscribeSprintSettings, getSprintSettingsSnapshot)
  const [open, setOpen] = useState(false)
  const [locations, setLocations] = useState<LocationItem[]>([])
  const [localReady, setLocalReady] = useState(false)

  // 打开面板时拉取真实系统目录与本机模型可用性
  useEffect(() => {
    if (!open) return
    fetch(`${OFFICE_API_BASE}/api/locations`)
      .then((res) => (res.ok ? res.json() : null))
      .then((payload: { locations?: LocationItem[] } | null) => setLocations(payload?.locations ?? []))
      .catch(() => undefined)
    fetch(`${OFFICE_API_BASE}/api/privacy/capabilities`)
      .then((res) => (res.ok ? res.json() : null))
      .then((payload: { local_model_ready?: boolean } | null) => setLocalReady(Boolean(payload?.local_model_ready)))
      .catch(() => setLocalReady(false))
  }, [open])

  return (
    <>
      <button
        type="button"
        className="sidebar-gear"
        title="任务设置：任务类型、生成流程、模型模式、保存位置与预算"
        aria-label="任务设置"
        onClick={() => setOpen(true)}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
        </svg>
      </button>
      {open
        ? createPortal(
            <div className="sprint-backdrop" onClick={() => setOpen(false)}>
              <div className="sprint-dialog settings-panel" onClick={(e) => e.stopPropagation()}>
                <header className="sprint-dialog-head">
                  <h3>任务设置</h3>
                  <button type="button" className="sprint-dialog-close" aria-label="关闭" onClick={() => setOpen(false)}>
                    ×
                  </button>
                </header>
                <p className="sprint-dialog-tip">这些设置会在对话页直接发送需求时生效。</p>
                <label className="sprint-fast-toggle">
                  <span>任务类型：</span>
                  <select
                    value={settings.taskKind}
                    onChange={(event) => {
                      const nextKind = event.target.value as 'auto' | 'file' | 'software'
                      updateSprintSettings(
                        nextKind === 'software'
                          ? { taskKind: nextKind, executionStyle: 'collaborative' }
                          : { taskKind: nextKind },
                      )
                    }}
                  >
                    <option value="auto">自动判断</option>
                    <option value="file">处理资料并生成文件</option>
                    <option value="software">开发软件项目</option>
                  </select>
                </label>
                <label className="sprint-fast-toggle">
                  <span>生成流程：</span>
                  <select
                    value={settings.executionStyle}
                    onChange={(event) => updateSprintSettings({ executionStyle: event.target.value as 'collaborative' | 'quick' })}
                  >
                    <option value="collaborative">六智能体协作（默认）</option>
                    <option value="quick" disabled={settings.taskKind === 'software'}>快速生成文件（不经过六人协作）</option>
                  </select>
                </label>
                {settings.executionStyle === 'quick' ? (
                  <p className="sprint-dialog-tip">快速模式由单名成员直接生成与检查文件，不展示六人协作记录。</p>
                ) : null}
                <label className="sprint-fast-toggle">
                  <span>模型模式：</span>
                  <select
                    value={settings.executionMode}
                    onChange={(event) => updateSprintSettings({ executionMode: event.target.value as 'cloud' | 'local' })}
                  >
                    <option value="cloud">已配置模型 API（附件内容会发送到该 API）</option>
                    <option value="local" disabled={!localReady}>仅本机模型 API{localReady ? '' : '（需先配置）'}</option>
                  </select>
                </label>
                <p className="sprint-dialog-tip">附件上传与资料库搜索在本机处理。执行模型任务时，只有本机模式限制请求发送到本机地址；请确认本机模型服务自身没有转发内容。</p>
                {locations.length > 0 ? (
                  <label className="sprint-fast-toggle">
                    <span>保存位置：</span>
                    <select
                      className="sprint-save-select"
                      value={settings.saveDir}
                      onChange={(event) => updateSprintSettings({ saveDir: event.target.value })}
                    >
                      <option value="">不指定（跟随需求描述）</option>
                      {locations.map((loc) => (
                        <option key={loc.path} value={loc.path}>
                          {loc.name}（{loc.path}）
                        </option>
                      ))}
                      <option value="__custom__">自定义路径…</option>
                    </select>
                  </label>
                ) : null}
                {settings.saveDir === '__custom__' ? (
                  <label className="sprint-fast-toggle">
                    <span>自定义路径：</span>
                    <input
                      className="sprint-budget-input sprint-custom-dir-input"
                      type="text"
                      placeholder="例如 D:\输出\周报 或 ~/Documents/成品"
                      value={settings.customDir}
                      onChange={(event) => updateSprintSettings({ customDir: event.target.value })}
                    />
                  </label>
                ) : null}
                <label className="sprint-fast-toggle">
                  <input
                    type="checkbox"
                    checked={settings.fast}
                    onChange={(event) => updateSprintSettings({ fast: event.target.checked })}
                  />
                  <span>极速模式：跳过办公室演出动画，更快拿到产物</span>
                </label>
                <label className="sprint-fast-toggle">
                  <span>花费上限（元）：</span>
                  <input
                    className="sprint-budget-input"
                    type="number"
                    min="0"
                    step="0.1"
                    placeholder="留空不限"
                    value={settings.budgetText}
                    onChange={(event) => updateSprintSettings({ budgetText: event.target.value })}
                  />
                  <span className="sprint-dialog-tip">超过上限自动收尾，不再继续调用模型</span>
                </label>
              </div>
            </div>,
            document.body,
          )
        : null}
    </>
  )
}
