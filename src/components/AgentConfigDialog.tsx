/** 成员设置弹窗：自定义姓名 + 配置该成员专属的 LLM API */

import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Application } from 'pixi.js'
import { Spine } from '@esotericsoftware/spine-pixi-v8'
import { OFFICE_API_BASE } from '../config/officeMode'
import {
  saveAgentConfig,
  type AgentConfig,
} from '../store/officeAgentConfigStore'
import { showToast } from '../store/officeUiStore'
import {
  CHIBI_CHARACTER_SKINS,
  CHIBI_SKIN_LABELS,
  resolveChibiSkin,
} from '@/scene/characters/chibiStickerSkins'
import {
  isSpineReady,
  getSpineSkeletonAlias,
  getSpineAtlasAlias,
} from '@/scene/assets/loadSpineAssets'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'

type TestResult =
  | { ok: true; reply: string; latency: number }
  | { ok: false; error: string }

type Props = {
  config: AgentConfig
  onClose: () => void
}

/** 主流 OpenAI 兼容服务商预设：选中后自动填入 Base URL 与推荐模型，Key 仍需自填 */
const PROVIDER_PRESETS = [
  { label: '自定义', base_url: '', model: '' },
  {
    label: 'DeepSeek',
    base_url: 'https://api.deepseek.com',
    model: 'deepseek-flash',
  },
  {
    label: '通义千问（阿里百炼）',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    model: 'qwen-plus',
  },
  {
    label: 'Kimi（月之暗面）',
    base_url: 'https://api.moonshot.cn/v1',
    model: 'kimi-k2.6',
  },
  {
    label: '智谱 GLM',
    base_url: 'https://open.bigmodel.cn/api/paas/v4',
    model: 'glm-4.7-flash',
  },
  {
    label: 'OpenAI',
    base_url: 'https://api.openai.com/v1',
    model: 'gpt-4o-mini',
  },
  {
    label: 'Ollama 本地',
    base_url: 'http://127.0.0.1:11434/v1',
    model: 'qwen2.5:7b',
  },
] as const

/** 可选头像 emoji：「默认」即空串，恢复色块首字母头像 */
const AVATAR_OPTIONS = [
  '🧑‍💼', '👩‍💼', '🧑‍💻', '👨‍💻', '👩‍💻', '🧑‍🎨', '👩‍🎨', '🧑‍🔬',
  '👨‍🔬', '🦊', '🐼', '🐯', '🦁', '🐸', '🐵', '🦄',
  '🐲', '🤖', '👽', '😺', '🐶', '🐰', '🐨', '🐷',
] as const

/** 形象预览：用主场景已加载的 Spine 资源在弹窗内渲染单个小人 */
function SkinPreview({ agentId, skin }: { agentId: string; skin: string }) {
  const hostRef = useRef<HTMLDivElement>(null)
  const appRef = useRef<Application | null>(null)
  const spineRef = useRef<Spine | null>(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    if (!isSpineReady() || !hostRef.current) return
    const app = new Application()
    let destroyed = false
    void app
      .init({ width: 96, height: 120, backgroundAlpha: 0, antialias: true })
      .then(() => {
        if (destroyed) {
          app.destroy(true)
          return
        }
        hostRef.current?.appendChild(app.canvas)
        appRef.current = app
        setReady(true)
      })
    return () => {
      destroyed = true
      setReady(false)
      appRef.current?.destroy(true)
      appRef.current = null
      spineRef.current = null
    }
  }, [])

  useEffect(() => {
    const app = appRef.current
    if (!app || !ready || !isSpineReady()) return
    if (spineRef.current) {
      app.stage.removeChild(spineRef.current)
      spineRef.current.destroy()
      spineRef.current = null
    }
    const skinName = resolveChibiSkin(agentId, skin)
    try {
      const sp = Spine.from({
        skeleton: getSpineSkeletonAlias(),
        atlas: getSpineAtlasAlias(),
        autoUpdate: true,
      })
      if (sp.skeleton.data.findSkin(skinName)) {
        sp.skeleton.setSkinByName(skinName)
        sp.skeleton.setSlotsToSetupPose()
      }
      sp.state.setAnimation(0, 'movement/idle-front', true)
      // 按包围盒自适应缩放，贴底居中
      const b = sp.getLocalBounds()
      const fit = Math.min(
        (app.screen.width - 6) / b.width,
        (app.screen.height - 6) / b.height,
        0.32,
      )
      sp.scale.set(fit)
      sp.position.set(
        app.screen.width / 2 - (b.x + b.width / 2) * fit,
        app.screen.height - 3 - (b.y + b.height) * fit,
      )
      app.stage.addChild(sp)
      spineRef.current = sp
    } catch {
      // 预览失败不阻塞选择
    }
  }, [agentId, skin, ready])

  if (!isSpineReady()) {
    return <div className="skin-preview-placeholder">形象资源加载中…</div>
  }
  return <div className="skin-preview-host" ref={hostRef} />
}

export function AgentConfigDialog({ config, onClose }: Props) {
  const [name, setName] = useState(config.name)
  const [avatar, setAvatar] = useState(config.avatar ?? '')
  const [skin, setSkin] = useState(config.skin ?? '')
  const [baseUrl, setBaseUrl] = useState(config.llm.base_url)
  // 后端不回传真实 Key：输入框默认留空，留空保存即保持原 Key 不变
  const [apiKey, setApiKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [model, setModel] = useState(config.llm.model)
  // 备用 API 区：交互与主 API 一致（留空保留旧 Key，「清除」提交 __CLEAR__）
  const [backupOpen, setBackupOpen] = useState(Boolean(config.has_backup_key))
  const [backupBaseUrl, setBackupBaseUrl] = useState(
    config.llm_backup?.base_url ?? '',
  )
  const [backupApiKey, setBackupApiKey] = useState('')
  const [showBackupKey, setShowBackupKey] = useState(false)
  const [backupModel, setBackupModel] = useState(config.llm_backup?.model ?? '')
  const [applyAll, setApplyAll] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<TestResult | null>(null)

  /** 用弹窗当前填写的值直接探测模型，不用先保存 */
  async function handleTest() {
    setTesting(true)
    setTestResult(null)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/agents/${config.roster}/test`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            base_url: baseUrl.trim(),
            api_key: apiKey.trim(),
            model: model.trim(),
          }),
        },
      )
      if (!res.ok) throw new Error(String(res.status))
      setTestResult((await res.json()) as TestResult)
    } catch {
      setTestResult({ ok: false, error: '请求失败，请确认后端已启动' })
    } finally {
      setTesting(false)
    }
  }

  async function handleSave() {
    const trimmed = name.trim()
    if (!trimmed) {
      showToast('姓名不能为空')
      return
    }
    setSaving(true)
    try {
      await saveAgentConfig(config.roster, {
        name: trimmed,
        avatar,
        skin,
        base_url: baseUrl.trim(),
        model: model.trim(),
        // Key 非空才提交（含未改动情况，值相同无害）；清空请用「清除 API Key」
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        backup_base_url: backupBaseUrl.trim(),
        backup_model: backupModel.trim(),
        ...(backupApiKey.trim() ? { backup_api_key: backupApiKey.trim() } : {}),
      })
      // 批量应用：把 API 配置（不含姓名）同步到其余成员，失败不中断
      if (applyAll) {
        const patch = {
          base_url: baseUrl.trim(),
          model: model.trim(),
          ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        }
        const others = [1, 2, 3, 4, 5, 6].filter((no) => no !== config.roster)
        const results = await Promise.allSettled(
          others.map((no) => saveAgentConfig(no, patch)),
        )
        const failed = results.filter((r) => r.status === 'rejected').length
        showToast(
          failed > 0
            ? `${trimmed} 已保存；批量应用时有 ${failed} 名成员失败，请逐个检查`
            : `${trimmed} 已保存，API 配置已同步到全部 6 名成员`,
        )
      } else {
        showToast(`${trimmed} 的设置已保存`)
      }
      onClose()
    } catch {
      showToast('保存失败，请确认后端已启动')
    } finally {
      setSaving(false)
    }
  }

  async function handleResetKey() {
    setSaving(true)
    try {
      await saveAgentConfig(config.roster, { api_key: '__CLEAR__' })
      showToast('已清除 API Key，该成员回退 Mock 兜底')
      onClose()
    } catch {
      showToast('操作失败，请确认后端已启动')
    } finally {
      setSaving(false)
    }
  }

  async function handleResetBackupKey() {
    setSaving(true)
    try {
      await saveAgentConfig(config.roster, { backup_api_key: '__CLEAR__' })
      showToast('已清除备用 API Key')
      onClose()
    } catch {
      showToast('操作失败，请确认后端已启动')
    } finally {
      setSaving(false)
    }
  }

  return createPortal(
    // 只能点右上角 × 关闭，点击空白处不关（防止误丢未保存的填写）
    <div className="sprint-backdrop">
      <div className="sprint-dialog agent-config-dialog">
        <header className="sprint-dialog-head">
          <h3>
            成员设置 · {config.role}
            {config.backup_in_use ? (
              <span
                className="backup-badge backup-badge-active"
                title="主 API 连续失败，已自动切换备用通道"
              >
                ⚠ 备用生效中
              </span>
            ) : config.has_backup_key ? (
              <span className="backup-badge">有备用</span>
            ) : null}
          </h3>
          <button
            type="button"
            className="sprint-dialog-close"
            aria-label="关闭"
            onClick={onClose}
          >
            ×
          </button>
        </header>

        <label className="config-field">
          <span>姓名</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="成员显示名"
            maxLength={20}
          />
        </label>

        <div className="config-field">
          <span>头像（显示在办公室名牌与各处成员头像上）</span>
          <div className="avatar-picker" role="radiogroup" aria-label="选择头像">
            <button
              type="button"
              className={`avatar-option avatar-option-default ${avatar === '' ? 'selected' : ''}`}
              title="默认（色块首字母）"
              aria-pressed={avatar === ''}
              onClick={() => setAvatar('')}
            >
              默认
            </button>
            {AVATAR_OPTIONS.map((emoji) => (
              <button
                key={emoji}
                type="button"
                className={`avatar-option ${avatar === emoji ? 'selected' : ''}`}
                aria-pressed={avatar === emoji}
                onClick={() => setAvatar(emoji)}
              >
                {emoji}
              </button>
            ))}
          </div>
        </div>

        <div className="config-field">
          <span>人物形象（办公室小人外观，右侧实时预览）</span>
          <div className="skin-picker-row">
            <div className="skin-options" role="radiogroup" aria-label="选择人物形象">
              <button
                type="button"
                className={`skin-option skin-option-default ${skin === '' ? 'selected' : ''}`}
                aria-pressed={skin === ''}
                onClick={() => setSkin('')}
              >
                默认
              </button>
              {CHIBI_CHARACTER_SKINS.map((id) => (
                <button
                  key={id}
                  type="button"
                  className={`skin-option ${skin === id ? 'selected' : ''}`}
                  aria-pressed={skin === id}
                  onClick={() => setSkin(id)}
                >
                  {CHIBI_SKIN_LABELS[id]}
                </button>
              ))}
            </div>
            <SkinPreview
              agentId={AGENT_ROSTER[config.roster - 1]?.id ?? ''}
              skin={skin}
            />
          </div>
        </div>

        <label className="config-field">
          <span>服务商预设（选中自动填入地址与模型）</span>
          <select
            defaultValue={
              PROVIDER_PRESETS.find((p) => p.base_url === config.llm.base_url)
                ?.label ?? '自定义'
            }
            onChange={(e) => {
              const preset = PROVIDER_PRESETS.find(
                (p) => p.label === e.target.value,
              )
              if (!preset || preset.label === '自定义') return
              setBaseUrl(preset.base_url)
              setModel(preset.model)
              setTestResult(null)
            }}
          >
            {PROVIDER_PRESETS.map((preset) => (
              <option key={preset.label} value={preset.label}>
                {preset.label}
              </option>
            ))}
          </select>
        </label>

        <label className="config-field">
          <span>Base URL（留空用默认 DeepSeek 地址）</span>
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://api.deepseek.com/v1"
          />
        </label>

        <label className="config-field">
          <span>API Key</span>
          <span className="config-key-row">
            <input
              type={showKey ? 'text' : 'password'}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={
                config.has_custom_key
                  ? `${config.llm.api_key_masked || '已配置'}（留空保持不变）`
                  : '未配置时该成员使用 Mock 兜底'
              }
            />
            <button
              type="button"
              className="config-key-eye"
              title={showKey ? '隐藏' : '显示'}
              onClick={() => setShowKey((v) => !v)}
            >
              {showKey ? '🙈' : '👁'}
            </button>
          </span>
        </label>

        <label className="config-field">
          <span>模型（留空用 deepseek-chat）</span>
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="deepseek-chat"
          />
        </label>

        <div className="config-backup">
          <button
            type="button"
            className="config-backup-toggle"
            aria-expanded={backupOpen}
            onClick={() => setBackupOpen((v) => !v)}
          >
            <span>{backupOpen ? '▾' : '▸'} 备用 API（主通道故障时自动切换）</span>
            {config.backup_in_use ? (
              <span
                className="backup-badge backup-badge-active"
                title="主 API 连续失败，已自动切换备用通道"
              >
                ⚠ 备用生效中
              </span>
            ) : config.has_backup_key ? (
              <span className="backup-badge">有备用</span>
            ) : null}
          </button>
          {backupOpen ? (
            <div className="config-backup-body">
              <label className="config-field">
                <span>备用 Base URL</span>
                <input
                  value={backupBaseUrl}
                  onChange={(e) => setBackupBaseUrl(e.target.value)}
                  placeholder="https://api.deepseek.com/v1"
                />
              </label>

              <label className="config-field">
                <span>备用 API Key</span>
                <span className="config-key-row">
                  <input
                    type={showBackupKey ? 'text' : 'password'}
                    value={backupApiKey}
                    onChange={(e) => setBackupApiKey(e.target.value)}
                    placeholder={
                      config.has_backup_key
                        ? `${config.llm_backup?.api_key_masked || '已配置'}（留空保持不变）`
                        : '未配置备用 Key'
                    }
                  />
                  <button
                    type="button"
                    className="config-key-eye"
                    title={showBackupKey ? '隐藏' : '显示'}
                    onClick={() => setShowBackupKey((v) => !v)}
                  >
                    {showBackupKey ? '🙈' : '👁'}
                  </button>
                </span>
              </label>

              <label className="config-field">
                <span>备用模型</span>
                <input
                  value={backupModel}
                  onChange={(e) => setBackupModel(e.target.value)}
                  placeholder="deepseek-chat"
                />
              </label>

              {config.has_backup_key ? (
                <button
                  type="button"
                  className="config-backup-clear"
                  disabled={saving}
                  onClick={handleResetBackupKey}
                >
                  清除备用 Key
                </button>
              ) : null}
            </div>
          ) : null}
        </div>

        <p className="config-tip">
          填入 API Key 后，该成员的所有思考和产出都走自己的模型服务；
          未配置的成员使用内置 Mock 兜底。系统已不提供全局 API。
        </p>

        <label className="config-apply-all">
          <input
            type="checkbox"
            checked={applyAll}
            onChange={(e) => setApplyAll(e.target.checked)}
          />
          <span>保存时将 API 配置（地址/模型/Key）批量应用到全部 6 名成员</span>
        </label>

        <div className="config-test-row">
          <button
            type="button"
            className="config-test-btn"
            disabled={testing}
            onClick={handleTest}
          >
            {testing ? '测试中…' : '⚡ 测试连接'}
          </button>
          {testResult ? (
            testResult.ok ? (
              <span className="config-test-success">
                ✓ 配置成功：模型回复「{testResult.reply}」（{testResult.latency}s）
              </span>
            ) : (
              <span className="config-test-error">✗ {testResult.error}</span>
            )
          ) : null}
        </div>

        <div className="sprint-dialog-actions">
          {config.has_custom_key ? (
            <button type="button" disabled={saving} onClick={handleResetKey}>
              清除 API Key
            </button>
          ) : null}
          <button
            type="button"
            className="primary"
            disabled={saving}
            onClick={handleSave}
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
