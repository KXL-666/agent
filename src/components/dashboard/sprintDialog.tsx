import { useEffect, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { AttachmentPicker } from '../AttachmentPicker'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { requestNotifyPermission } from '@/services/officeNotify'
import { getSprintSettingsSnapshot, startSprint } from '@/store/officeSprintSettings'
import type { OfficeUpload } from '@/store/officeUploadStore'
import {
  isSprintRunning,
  subscribeSprintData,
} from '@/store/officeTaskStore'
import {
  closeSprintDialog,
  takeSprintPrefill,
} from '@/store/officeViewStore'
import { showToast } from '@/store/officeUiStore'
import type { AgentApiStatus } from './taskPanels'

type LocationItem = { name: string; path: string }

type SprintPlan = { route: string; output: string; attachments: OfficeUpload[]; save_dir: string; budget: number; data_notice: string }

/** 新建 Sprint 弹窗的示例需求：点击填入输入框 */
const SPRINT_EXAMPLES: { label: string; text: string }[] = [
  {
    label: '校园生活作文',
    text: '写一份关于「我的校园生活」的800字作文，生成Word文档',
  },
  {
    label: '团队项目周报',
    text: '生成一份团队项目周报Word文档，保存到桌面',
  },
  {
    label: 'AI 产品 PPT',
    text: '制作一份「AI产品介绍」PPT，8页',
  },
  {
    label: '待办 HTML 应用',
    text: '开发一个单文件HTML待办事项管理应用，可直接浏览器打开',
  },
]

export function SprintDialog() {
  const running = useSyncExternalStore(subscribeSprintData, isSprintRunning)
  const [requirement, setRequirement] = useState(() => takeSprintPrefill())
  const [submitting, setSubmitting] = useState(false)
  const [demoStarting, setDemoStarting] = useState(false)
  const [attachments, setAttachments] = useState<OfficeUpload[]>([])
  // 默认值与侧栏 ⚙ 任务设置保持一致
  const [fast, setFast] = useState(() => getSprintSettingsSnapshot().fast)
  const [saveDir, setSaveDir] = useState(() => getSprintSettingsSnapshot().saveDir)
  const [customDir, setCustomDir] = useState(() => getSprintSettingsSnapshot().customDir)
  const [locations, setLocations] = useState<LocationItem[]>([])
  const [budgetText, setBudgetText] = useState(() => getSprintSettingsSnapshot().budgetText)
  const [taskKind, setTaskKind] = useState<'auto' | 'file' | 'software'>(() => getSprintSettingsSnapshot().taskKind)
  const [executionMode, setExecutionMode] = useState<'cloud' | 'local'>(() => getSprintSettingsSnapshot().executionMode)
  const [executionStyle, setExecutionStyle] = useState<'collaborative' | 'quick'>(() => getSprintSettingsSnapshot().executionStyle)
  const [plan, setPlan] = useState<SprintPlan | null>(null)
  const [planSignature, setPlanSignature] = useState('')
  const [localReady, setLocalReady] = useState(false)
  const [agentStatus, setAgentStatus] = useState<AgentApiStatus[]>([])

  // 拉取真实系统目录（桌面/文档/下载等），供保存位置下拉选择
  useEffect(() => {
    async function load() {
      try {
        const res = await fetch(`${OFFICE_API_BASE}/api/locations`)
        if (!res.ok) return
        const payload = (await res.json()) as { locations?: LocationItem[] }
        setLocations(payload.locations ?? [])
      } catch {
        // 后端未启动时隐藏该选项即可
      }
      // 开工前 API 体检：列出未配置专属 Key 的成员，避免静默 Mock
      try {
        const res = await fetch(`${OFFICE_API_BASE}/api/agents`)
        if (!res.ok) return
        const payload = (await res.json()) as { agents?: AgentApiStatus[] }
        setAgentStatus(payload.agents ?? [])
      } catch {
        // 体检失败不阻断开工
      }
      try {
        const response = await fetch(`${OFFICE_API_BASE}/api/privacy/capabilities`)
        const result = await response.json() as { local_model_ready?: boolean }
        setLocalReady(Boolean(result.local_model_ready))
      } catch { setLocalReady(false) }
    }
    void load()
  }, [])

  const mockAgents = agentStatus.filter((a) => !a.has_custom_key)

  async function handleSubmit() {
    const text = requirement.trim()
    if (!text) {
      showToast('请先描述需求')
      return
    }
    const budget = budgetText.trim() ? Number(budgetText) : 0
    if (budgetText.trim() && (!Number.isFinite(budget) || budget < 0)) {
      showToast('预算上限请输入非负数字（元），留空表示不限')
      return
    }
    // 自定义路径：select 选「自定义」时以手填路径为准
    const effectiveSaveDir = saveDir === '__custom__' ? customDir.trim() : saveDir
    if (saveDir === '__custom__' && !effectiveSaveDir) {
      showToast('请填写自定义保存路径，或改选其他保存位置')
      return
    }
    const payload = { requirement: text, upload_ids: attachments.map(item => item.id), fast, save_dir: effectiveSaveDir, budget, task_kind: taskKind, execution_mode: executionMode, execution_style: executionStyle }
    const signature = JSON.stringify(payload)
    if (!plan || planSignature !== signature) {
      setSubmitting(true)
      try {
        const response = await fetch(`${OFFICE_API_BASE}/api/sprint/plan`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: signature })
        const result = await response.json() as SprintPlan & { detail?: string }
        if (!response.ok) { showToast(result.detail || '计划生成失败'); return }
        setPlan(result)
        setPlanSignature(signature)
      } catch { showToast('计划生成失败，请确认后端已启动') }
      finally { setSubmitting(false) }
      return
    }
    requestNotifyPermission()
    setSubmitting(true)
    const error = await startSprint(
      text,
      attachments.map((item) => item.id),
      fast,
      effectiveSaveDir,
      budget,
      taskKind,
      executionMode,
      executionStyle,
    )
    setSubmitting(false)
    if (error) {
      showToast(error)
      return
    }
    showToast('任务已启动，可在「任务」查看进度')
    closeSprintDialog()
  }

  /** 演示模式：脚本化 Sprint，零 API 消耗，无需任何 Key */
  async function handleDemo() {
    requestNotifyPermission()
    setDemoStarting(true)
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/demo/start`, {
        method: 'POST',
      })
      if (res.status === 409) {
        showToast('已有 Sprint 进行中')
        return
      }
      if (!res.ok) {
        const detail = (await res.json().catch(() => null)) as {
          detail?: string
        } | null
        showToast(detail?.detail ?? '演示启动失败')
        return
      }
      showToast('🎬 演示 Sprint 已启动（零 API 消耗），看办公室演出吧')
      closeSprintDialog()
    } catch {
      showToast('无法连接后端，请确认后端已启动')
    } finally {
      setDemoStarting(false)
    }
  }

  const currentSignature = JSON.stringify({
    requirement: requirement.trim(),
    upload_ids: attachments.map((item) => item.id),
    fast,
    save_dir: saveDir === '__custom__' ? customDir.trim() : saveDir,
    budget: budgetText.trim() ? Number(budgetText) : 0,
    task_kind: taskKind,
    execution_mode: executionMode,
    execution_style: executionStyle,
  })
  const planReady = plan !== null && planSignature === currentSignature

  // Portal 挂到 body：避开底部工具栏的 transform，
  // 否则 fixed 遮罩会被裁剪在工具栏区域内导致弹窗显示不全
  return createPortal(
    <div className="sprint-backdrop" onClick={closeSprintDialog}>
      <div className="sprint-dialog" onClick={(e) => e.stopPropagation()}>
        <header className="sprint-dialog-head">
          <h3>{planReady ? '确认执行计划' : '交代一件事'}</h3>
          <button
            type="button"
            className="sprint-dialog-close"
            aria-label="关闭"
            onClick={closeSprintDialog}
          >
            ×
          </button>
        </header>
        {!planReady ? (
          <>
        <label className="sprint-input-label" htmlFor="sprint-requirement">你想完成什么？</label>
        <textarea
          id="sprint-requirement"
          autoFocus
          placeholder="说出你想完成的事，例如：根据附件写一份项目总结 Word"
          value={requirement}
          onChange={(e) => setRequirement(e.target.value)}
        />
        <span className="sprint-dialog-tip">只需描述结果，例如“根据附件写一份项目总结 Word”。稍后可以先核对执行计划。</span>
        <div className="sprint-example-chips">
          <span className="sprint-dialog-tip">没有头绪？试试：</span>
          {SPRINT_EXAMPLES.map((example) => (
            <button
              key={example.label}
              type="button"
              title={example.text}
              onClick={() => setRequirement(example.text)}
            >
              {example.label}
            </button>
          ))}
        </div>
        <div className="sprint-attachment-row">
          <span>相关资料（可选）</span>
          <AttachmentPicker value={attachments} onChange={setAttachments} />
        </div>
        <p className="sprint-privacy-note">
          {executionMode === 'cloud'
            ? '任务内容与附件会发送到已配置的模型 API；预览计划不会启动模型任务。'
            : '任务仅调用本机模型 API；预览计划不会启动模型任务。'}
        </p>
        {mockAgents.length > 0 ? (
          <p className="sprint-setup-note">{mockAgents.length} 位成员尚未配置专属 API，可能使用模板内容。可在下方“更多设置”中查看。</p>
        ) : null}
        <details className="sprint-advanced">
          <summary>更多设置 <span>任务类型、模型、保存位置与预算</span></summary>
          <p>预算按已返回的模型用量估算；并行请求和服务商实际计费可能使最终费用超出设置值。</p>
        <label className="sprint-fast-toggle"><span>任务类型：</span><select value={taskKind} onChange={event => {
          const nextKind = event.target.value as 'auto' | 'file' | 'software'
          setTaskKind(nextKind)
          if (nextKind === 'software') setExecutionStyle('collaborative')
        }}>
          <option value="auto">自动判断</option><option value="file">处理资料并生成文件</option><option value="software">开发软件项目</option>
        </select></label>
        <label className="sprint-fast-toggle"><span>生成流程：</span><select value={executionStyle} onChange={event => setExecutionStyle(event.target.value as 'collaborative' | 'quick')}>
          <option value="collaborative">六智能体协作（默认）</option>
          <option value="quick" disabled={taskKind === 'software'}>快速生成文件（不经过六人协作）</option>
        </select></label>
        {executionStyle === 'quick' ? <p className="sprint-dialog-tip">快速模式由单名成员直接生成与检查文件，不展示六人协作记录。</p> : null}
        {agentStatus.length > 0 && mockAgents.length > 0 ? (
          <p className="sprint-api-warning">
            ⚠ 以下成员未配置专属 API，将使用 Mock 兜底（产出为模板内容）：
            {mockAgents.map((a) => `${a.role}·${a.name}`).join('、')}
            。可在「团队 → ⚙ 设置」中补齐。
          </p>
        ) : null}
        <label className="sprint-fast-toggle"><span>模型模式：</span><select value={executionMode} onChange={event => setExecutionMode(event.target.value as 'cloud' | 'local')}>
          <option value="cloud">已配置模型 API（附件内容会发送到该 API）</option>
          <option value="local" disabled={!localReady}>仅本机模型 API{localReady ? '' : '（需先配置）'}</option>
        </select></label>
        <p className="sprint-dialog-tip">附件上传与资料库搜索在本机处理。执行模型任务时，只有本机模式限制请求发送到本机地址；请确认本机模型服务自身没有转发内容。</p>
        {locations.length > 0 ? (
          <label className="sprint-fast-toggle">
            <span>保存位置：</span>
            <select
              className="sprint-save-select"
              value={saveDir}
              onChange={(event) => setSaveDir(event.target.value)}
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
        {saveDir === '__custom__' ? (
          <label className="sprint-fast-toggle">
            <span>自定义路径：</span>
            <input
              className="sprint-budget-input sprint-custom-dir-input"
              type="text"
              placeholder="例如 D:\输出\周报 或 ~/Documents/成品"
              value={customDir}
              onChange={(event) => setCustomDir(event.target.value)}
            />
          </label>
        ) : null}
        <label className="sprint-fast-toggle">
          <input
            type="checkbox"
            checked={fast}
            onChange={(event) => setFast(event.target.checked)}
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
            value={budgetText}
            onChange={(event) => setBudgetText(event.target.value)}
          />
          <span className="sprint-dialog-tip">超过上限自动收尾，不再继续调用模型</span>
        </label>
        </details>
          </>
        ) : null}
        {planReady ? (
          <section className="sprint-plan" aria-label="执行计划预览">
            <strong>执行前请核对</strong>
            <dl>
              <div><dt>处理方式</dt><dd>{plan.route}</dd></div>
              <div><dt>任务内容</dt><dd>{requirement.trim()}</dd></div>
              <div><dt>保存位置</dt><dd>{plan.save_dir}</dd></div>
              <div><dt>输入资料</dt><dd>{plan.attachments.length ? plan.attachments.map((item) => item.name).join('、') : '仅需求文字'}</dd></div>
              <div><dt>花费上限</dt><dd>{plan.budget > 0 ? `¥${plan.budget.toFixed(2)}` : '未设置'}</dd></div>
            </dl>
            <p className="sprint-plan-notice">{executionMode === 'cloud' ? '确认启动后，任务内容与附件会发送到已配置的模型 API。' : plan.data_notice}</p>
          </section>
        ) : null}
        <div className="sprint-dialog-actions">
          {planReady ? (
            <button type="button" className="sprint-back-btn" onClick={() => { setPlan(null); setPlanSignature('') }}>返回修改</button>
          ) : (
          <button
            type="button"
            className="demo-btn"
            disabled={running || submitting || demoStarting}
            title="脚本化演示 Sprint，不调用任何模型 API"
            onClick={() => void handleDemo()}
          >
            {demoStarting ? '启动中…' : '先看演示（零 API 消耗）'}
          </button>
          )}
          <button
            type="button"
            className="primary"
            disabled={running || submitting || demoStarting || !requirement.trim()}
            onClick={handleSubmit}
          >
            {submitting ? '处理中…' : planReady ? '确认并启动' : '下一步：预览计划'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
