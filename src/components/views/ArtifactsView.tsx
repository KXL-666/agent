import { useEffect, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { RevisionChatPanel } from '@/components/RevisionChatPanel'
import { ArtifactQualityPanel } from '@/components/ArtifactQualityPanel'
import { OFFICE_API_BASE } from '@/config/officeMode'
import {
  getArtifactsSnapshot,
  removeArtifact,
  subscribeArtifacts,
  upsertArtifact,
  type OfficeArtifact,
} from '@/store/officeArtifactStore'
import { showToast } from '@/store/officeUiStore'
import { assigneeName } from '@/store/officeTaskStore'
import { UsagePanel } from './UsagePanel'
import {
  canPreviewFile,
  diffLines,
  downloadArtifact,
  fileExt,
  qualityLabel,
  sourceLabel,
  timeLabel,
} from './officeViewUtils'

/** 版本链面板：展示同一产物的全部历史版本，支持查看内容、相邻版本对比与回滚 */
function VersionChainPanel({
  artifact,
  onClose,
}: {
  artifact: OfficeArtifact
  onClose: () => void
}) {
  const [versions, setVersions] = useState<OfficeArtifact[]>([])
  const [loading, setLoading] = useState(true)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [diffFor, setDiffFor] = useState<string | null>(null)
  const [rolling, setRolling] = useState(false)

  async function load() {
    setLoading(true)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/versions`,
      )
      if (!res.ok) throw new Error(String(res.status))
      const payload = (await res.json()) as { versions?: OfficeArtifact[] }
      setVersions(payload.versions ?? [])
    } catch {
      showToast('加载版本链失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifact.id])

  async function rollback(target: OfficeArtifact) {
    if (rolling) return
    setRolling(true)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/rollback`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target_id: target.id }),
        },
      )
      const payload = (await res.json().catch(() => null)) as
        | { artifact?: OfficeArtifact; detail?: string }
        | null
      if (!res.ok) {
        showToast(payload?.detail ?? '回滚失败')
        return
      }
      if (payload?.artifact) upsertArtifact(payload.artifact)
      showToast(`已按 v${target.version ?? 1} 的内容生成回滚版本，旧版仍保留`)
      await load()
    } catch {
      showToast('回滚失败，请确认后端已启动')
    } finally {
      setRolling(false)
    }
  }

  const latestId = versions[versions.length - 1]?.id

  return (
    <div className="version-chain-panel">
      <header>
        <strong>版本链（{versions.length} 个版本）</strong>
        <button type="button" onClick={onClose}>
          收起
        </button>
      </header>
      {loading ? <p className="view-card-meta">加载中…</p> : null}
      {!loading &&
        versions.map((v, index) => {
          const prev = index > 0 ? versions[index - 1] : null
          const showDiff = diffFor === v.id && prev
          return (
            <div
              key={v.id}
              className={`version-row ${v.id === latestId ? 'latest' : ''}`}
            >
              <div className="version-row-head">
                <span className="artifact-file-badge">v{v.version ?? 1}</span>
                <strong>{v.title}</strong>
                <em>{timeLabel(v.created_at)}</em>
                {v.id === latestId ? (
                  <span className="version-latest-badge">当前最新</span>
                ) : null}
              </div>
              <div className="version-row-actions">
                <button
                  type="button"
                  onClick={() =>
                    setExpandedId(expandedId === v.id ? null : v.id)
                  }
                >
                  {expandedId === v.id ? '收起内容' : '查看内容'}
                </button>
                {prev ? (
                  <button
                    type="button"
                    onClick={() => setDiffFor(showDiff ? null : v.id)}
                  >
                    {showDiff ? '收起对比' : `对比 v${prev.version ?? index}`}
                  </button>
                ) : null}
                {v.id !== latestId ? (
                  <button
                    type="button"
                    disabled={rolling}
                    onClick={() => void rollback(v)}
                  >
                    {rolling ? '回滚中…' : '回滚到此版'}
                  </button>
                ) : null}
              </div>
              {expandedId === v.id ? (
                <pre className="view-md version-content">{v.content}</pre>
              ) : null}
              {showDiff && prev ? (
                <pre className="version-diff">
                  {diffLines(prev.content, v.content).map((line, i) => (
                    <span key={i} className={`diff-${line.type}`}>
                      {line.type === 'add' ? '+ ' : line.type === 'del' ? '- ' : '  '}
                      {line.text}
                      {'\n'}
                    </span>
                  ))}
                </pre>
              ) : null}
            </div>
          )
        })}
    </div>
  )
}

/** 产物内容编辑弹窗：载入原始全文，保存为版本链上的新「编辑 vN」版本 */
function ArtifactEditModal({
  artifact,
  onClose,
}: {
  artifact: OfficeArtifact
  onClose: () => void
}) {
  const [content, setContent] = useState('')
  const [truncated, setTruncated] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await fetch(
          `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/raw`,
        )
        if (!res.ok) throw new Error(String(res.status))
        const payload = (await res.json()) as {
          content: string
          truncated: boolean
        }
        if (cancelled) return
        setContent(payload.content)
        setTruncated(Boolean(payload.truncated))
      } catch {
        if (!cancelled) showToast('加载产物内容失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [artifact.id])

  async function save() {
    if (saving) return
    setSaving(true)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/edit`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content }),
        },
      )
      const payload = (await res.json().catch(() => null)) as {
        detail?: string
      } | null
      if (!res.ok) {
        showToast(payload?.detail ?? '保存失败')
        return
      }
      showToast('已保存为新版本', { kind: 'success' })
      onClose()
    } catch {
      showToast('保存失败，请确认后端已启动')
    } finally {
      setSaving(false)
    }
  }

  const isDocx = Boolean(artifact.file_name?.toLowerCase().match(/\.(docx|pdf)$/))

  return createPortal(
    <div className="sprint-backdrop" onClick={onClose}>
      <div
        className="sprint-dialog artifact-edit-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="sprint-dialog-head">
          <h3>编辑：{artifact.title}</h3>
          <button
            type="button"
            className="sprint-dialog-close"
            aria-label="关闭"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        {truncated ? (
          <p className="artifact-edit-warning">
            ⚠ 内容超200KB被截断，保存将截断版本
          </p>
        ) : null}
        {isDocx ? (
          <p className="sprint-dialog-tip">
            保存后将按原格式重新生成 Word 或 PDF 文件（支持 Markdown 标题和列表）
          </p>
        ) : null}
        {loading ? (
          <p className="view-card-meta">内容加载中…</p>
        ) : (
          <textarea
            className="artifact-edit-textarea"
            value={content}
            onChange={(event) => setContent(event.target.value)}
          />
        )}
        <div className="sprint-dialog-actions">
          <button type="button" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary"
            disabled={loading || saving}
            onClick={() => void save()}
          >
            {saving ? '保存中…' : '保存为新版本'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

/** HTML 产物沙箱预览：iframe 仅允许脚本，隔离文档域 */
function ArtifactSandboxModal({
  artifact,
  onClose,
}: {
  artifact: OfficeArtifact
  onClose: () => void
}) {
  return createPortal(
    <div className="sprint-backdrop" onClick={onClose}>
      <div
        className="sprint-dialog artifact-sandbox-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="sprint-dialog-head">
          <h3>沙箱预览：{artifact.title}</h3>
          <button
            type="button"
            className="sprint-dialog-close"
            aria-label="关闭"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <iframe
          className="artifact-sandbox-iframe"
          sandbox="allow-scripts"
          title={`沙箱预览：${artifact.title}`}
          src={`${OFFICE_API_BASE}/api/artifacts/${artifact.id}/code_preview`}
        />
      </div>
    </div>,
    document.body,
  )
}

export function ArtifactsView() {
  const [error, setError] = useState<string | null>(null)
  const [loadingAll, setLoadingAll] = useState(true)
  const [allArtifacts, setAllArtifacts] = useState<OfficeArtifact[]>([])
  const [artifactQuery, setArtifactQuery] = useState('')
  const [openId, setOpenId] = useState<string | null>(null)
  const [qualityFor, setQualityFor] = useState<string | null>(null)
  const [editFor, setEditFor] = useState<OfficeArtifact | null>(null)
  const [sandboxFor, setSandboxFor] = useState<OfficeArtifact | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [deletingGroup, setDeletingGroup] = useState<string | null>(null)
  const [revisionFor, setRevisionFor] = useState<OfficeArtifact | null>(null)
  const [conversationOpen, setConversationOpen] = useState(false)
  const [versionsFor, setVersionsFor] = useState<string | null>(null)
  const currentArtifacts = useSyncExternalStore(subscribeArtifacts, getArtifactsSnapshot)

  async function refreshDeliverables() {
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/artifacts/deliverables`)
      if (!res.ok) throw new Error(String(res.status))
      const payload = (await res.json()) as {
        artifacts?: Array<OfficeArtifact & { sprint_requirement?: string; sprint_started_at?: number }>
      }
      setAllArtifacts(payload.artifacts ?? [])
      setError(null)
      return true
    } catch {
      setError('无法加载历史产物，请确认后端已启动')
      return false
    } finally {
      setLoadingAll(false)
    }
  }

  useEffect(() => {
    void refreshDeliverables()
    // 首次挂载读取跨 Sprint 的最终交付物。
  }, [])

  useEffect(() => {
    if (currentArtifacts.length > 0) void refreshDeliverables()
    // WebSocket updates the current Sprint artifact store when a final file is generated.
  }, [currentArtifacts])

  const mine = allArtifacts.filter((artifact) => artifact.kind === 'final' || artifact.kind === 'custom' || artifact.sprint_id === 'solo')
  const grouped = new Map<string, { id: string; date: string; requirement: string; items: OfficeArtifact[] }>()
  for (const artifact of mine) {
    const item = artifact as OfficeArtifact & { sprint_requirement?: string; sprint_started_at?: number }
    const timestamp = item.sprint_started_at || artifact.created_at
    const date = timestamp ? new Date(timestamp * 1000).toLocaleDateString('zh-CN') : '日期未知'
    const requirement = artifact.sprint_id === 'solo'
      ? '单飞 · 成员独立产出'
      : item.sprint_requirement?.trim() || '独立生成'
    const key = artifact.sprint_id || artifact.id
    const group = grouped.get(key) ?? { id: key, date, requirement, items: [] }
    group.items.push(artifact)
    grouped.set(key, group)
  }
  const query = artifactQuery.trim().toLocaleLowerCase()
  const groups = [...grouped.values()].filter((group) =>
    !query || group.requirement.toLocaleLowerCase().includes(query) ||
    group.items.some((artifact) => artifact.title.toLocaleLowerCase().includes(query)),
  )
  for (const group of groups) {
    group.items.sort((a, b) => a.title.localeCompare(b.title, 'zh-CN') || b.created_at - a.created_at)
  }

  function download(artifact: OfficeArtifact) {
    downloadArtifact(artifact)
  }

  async function copy(artifact: OfficeArtifact) {
    try {
      await navigator.clipboard.writeText(artifact.content)
      showToast('全文已复制到剪贴板')
    } catch {
      showToast('复制失败，请手动选择文本复制')
    }
  }

  async function retry(artifact: OfficeArtifact) {
    if (busyId) return
    setBusyId(artifact.id)
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/retry`,
        { method: 'POST' },
      )
      const payload = (await res.json().catch(() => null)) as
        | { artifact?: OfficeArtifact; detail?: string }
        | null
      if (!res.ok) {
        showToast(payload?.detail ?? '重新生成失败')
        return
      }
      if (payload?.artifact) upsertArtifact(payload.artifact)
      await refreshDeliverables()
      showToast('已重新生成')
    } catch {
      showToast('重新生成失败，请确认后端已启动')
    } finally {
      setBusyId(null)
    }
  }

  async function reveal(artifact: OfficeArtifact) {
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/reveal`,
        { method: 'POST' },
      )
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        showToast(typeof detail?.detail === 'string' ? detail.detail : '打开失败')
      }
    } catch {
      showToast('打开所在文件夹失败，请确认后端已启动')
    }
  }

  /** 结构化预览：Word/PPT/Excel 在后端解析为 HTML 新标签页展示 */
  function preview(artifact: OfficeArtifact) {
    window.open(
      `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/preview`,
      '_blank',
      'noopener',
    )
  }

  async function removeOne(artifact: OfficeArtifact) {
    const ok = window.confirm(
      `确定删除产物「${artifact.title}」？其磁盘文件将一并删除，不可恢复。`,
    )
    if (!ok) return
    try {
      const res = await fetch(
        `${OFFICE_API_BASE}/api/artifacts/${artifact.id}`,
        { method: 'DELETE' },
      )
      const detail = (await res.json().catch(() => null)) as {
        detail?: string
      } | null
      if (!res.ok) {
        showToast(detail?.detail ?? '删除失败')
        return
      }
      removeArtifact(artifact.id)
      setAllArtifacts((items) => items.filter((item) => item.id !== artifact.id))
      showToast('产物已删除')
    } catch {
      showToast('删除失败，请确认后端已启动')
    }
  }

  async function removeGroup(group: { id: string; requirement: string; items: OfficeArtifact[] }) {
    if (deletingGroup) return
    if (!window.confirm(`确定删除任务「${group.requirement}」的所有交付产物？对应磁盘文件也会删除，且不可恢复。`)) return
    setDeletingGroup(group.id)
    const deletedIds = new Set<string>()
    let failed = false
    try {
      for (const artifact of group.items) {
        const res = await fetch(`${OFFICE_API_BASE}/api/artifacts/${artifact.id}`, { method: 'DELETE' })
        if (!res.ok) { failed = true; break }
        removeArtifact(artifact.id)
        deletedIds.add(artifact.id)
      }
    } catch {
      failed = true
    } finally {
      setAllArtifacts((items) => items.filter((item) => !deletedIds.has(item.id)))
      setDeletingGroup(null)
      showToast(failed ? '部分交付删除失败，未删除的仍保留在该任务下' : '该任务的交付产物已删除')
    }
  }

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>最终交付</h1>
          <p>按日期和需求名称归档成品；开发、测试、评审及复盘记录可在「过程文件」查看。</p>
        </div>
        <button type="button" className="view-primary-btn" onClick={() => { setRevisionFor(null); setConversationOpen(true) }}>对话生成与修改</button>
      </header>

      <label className="artifact-history-search">
        <span>搜索产物</span>
        <input
          value={artifactQuery}
          onChange={(event) => setArtifactQuery(event.target.value)}
          placeholder="按产物名称或需求名称搜索"
        />
      </label>

      <UsagePanel />

      {error ? <div className="view-empty">{error}</div> : null}

      {!error ? (
        loadingAll ? (
          <div className="view-empty">正在加载历史产物…</div>
        ) : groups.length === 0 ? (
          <div className="view-empty">
            {artifactQuery ? '没有匹配的最终产物。' : '暂无最终产物。完成 Sprint 后，成品会归档在这里。'}
          </div>
        ) : (
          <div className="artifact-history-groups">
            {groups.map((group) => (
              <div className="artifact-history-group" key={group.id}>
              <details className="artifact-history-entry">
                <summary>
                  <time>{group.date}</time>
                  <h2>{group.requirement}</h2>
                  <span className="artifact-history-chevron" aria-hidden="true" />
                </summary>
                <div className="artifact-doc-list">
            {group.items.map((artifact) => {
              const open = openId === artifact.id
              const degraded = Boolean(artifact.degraded)
              const previewable = canPreviewFile(artifact)
              const legacyDemo = (artifact as OfficeArtifact & { sprint_requirement?: string }).sprint_requirement?.startsWith('【演示模式】') ?? false
              return (
                <article
                  key={artifact.id}
                  className={`artifact-doc ${degraded ? 'artifact-degraded' : ''}`}
                >
                  <div className="artifact-doc-head">
                    <button
                      type="button"
                      className="artifact-doc-toggle"
                      aria-expanded={open}
                      onClick={() => setOpenId(open ? null : artifact.id)}
                    >
                      <strong>{artifact.title}</strong>
                      <span className="artifact-doc-badges">
                        {artifact.sprint_id === 'solo' ? (
                          <span className="artifact-file-badge">单飞 · {assigneeName(artifact.author_roster)}</span>
                        ) : null}
                        <span className="artifact-source-badge">{sourceLabel(artifact, legacyDemo)}</span>
                        <span className={`artifact-quality-badge ${artifact.quality_status === 'failed' ? 'is-failed' : ''}`}>{qualityLabel(artifact)}</span>
                        <span className="artifact-file-badge">{artifact.file_name ? `文件可下载 ${fileExt(artifact) ?? ''}` : '仅文字摘要'}</span>
                        {artifact.version && artifact.version > 1 ? <span className="artifact-file-badge">v{artifact.version}</span> : null}
                      </span>
                      <em>{timeLabel(artifact.created_at)}</em>
                    </button>
                    <div className="artifact-doc-quick-actions">
                      {artifact.file_name || artifact.kind === 'final' ? <button type="button" aria-expanded={qualityFor === artifact.id} onClick={() => setQualityFor(qualityFor === artifact.id ? null : artifact.id)}>验收详情</button> : null}
                      {previewable ? (
                        artifact.file_name?.toLowerCase().endsWith('.html') ? (
                          <button type="button" onClick={() => setSandboxFor(artifact)}>预览</button>
                        ) : (
                          <button type="button" onClick={() => preview(artifact)}>预览</button>
                        )
                      ) : null}
                      {!artifact.file_name ? <button type="button" onClick={() => setOpenId(open ? null : artifact.id)}>查看摘要</button> : null}
                      <button type="button" className="artifact-quick-download" onClick={() => download(artifact)}>{artifact.file_name ? '下载文件' : '下载摘要'}</button>
                      {artifact.file_name ? <button type="button" onClick={() => void reveal(artifact)}>打开所在文件夹</button> : null}
                      <button type="button" className="artifact-revise-btn" onClick={() => setRevisionFor(artifact)}>对话修改</button>
                      <button type="button" className="artifact-delete-btn" onClick={() => void removeOne(artifact)}>删除</button>
                    </div>
                  </div>
                  {qualityFor === artifact.id ? <ArtifactQualityPanel artifactId={artifact.id} /> : null}
                  {open ? (
                    <>
                      {degraded && artifact.degraded_reason ? (
                        <p className="artifact-degraded-tip">
                          ⚠ 内容或文件存在问题：{artifact.degraded_reason}
                        </p>
                      ) : null}
                      <pre className="view-md artifact-doc-body">
                        {artifact.content}
                      </pre>
                      {versionsFor === artifact.id ? (
                        <VersionChainPanel
                          artifact={artifact}
                          onClose={() => setVersionsFor(null)}
                        />
                      ) : null}
                      <footer>
                        <button type="button" onClick={() => copy(artifact)}>
                          复制全文
                        </button>
                        <button
                          type="button"
                          className="artifact-edit-btn"
                          onClick={() => setEditFor(artifact)}
                        >
                          ✏️ 编辑
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            setVersionsFor(
                              versionsFor === artifact.id ? null : artifact.id,
                            )
                          }
                        >
                          {versionsFor === artifact.id ? '收起版本' : '版本'}
                        </button>
                        <button
                          type="button"
                          className="artifact-retry-btn"
                          disabled={busyId === artifact.id}
                          onClick={() => void retry(artifact)}
                        >
                          {busyId === artifact.id ? '重试中…' : '重新生成'}
                        </button>
                        <button
                          type="button"
                          className="artifact-revise-btn"
                          onClick={() => setRevisionFor(artifact)}
                        >
                          对话修改
                        </button>
                        <button
                          type="button"
                          className="artifact-delete-btn"
                          onClick={() => void removeOne(artifact)}
                        >
                          删除
                        </button>
                      </footer>
                    </>
                  ) : null}
                </article>
              )
            })}
                </div>
              </details>
              <button type="button" className="artifact-history-delete artifact-delete-btn" disabled={deletingGroup !== null} aria-label={`删除任务「${group.requirement}」的所有交付产物`} onClick={() => void removeGroup(group)}>{deletingGroup === group.id ? '删除中…' : '删除'}</button>
              </div>
            ))}
          </div>
        )
      ) : null}
      {revisionFor || conversationOpen ? <RevisionChatPanel artifact={revisionFor ?? undefined} onClose={() => { setRevisionFor(null); setConversationOpen(false); void refreshDeliverables() }} /> : null}
      {editFor ? (
        <ArtifactEditModal artifact={editFor} onClose={() => { setEditFor(null); void refreshDeliverables() }} />
      ) : null}
      {sandboxFor ? (
        <ArtifactSandboxModal
          artifact={sandboxFor}
          onClose={() => setSandboxFor(null)}
        />
      ) : null}
    </div>
  )
}
