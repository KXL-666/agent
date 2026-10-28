import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { OFFICE_API_BASE } from '@/config/officeMode'
import type { OfficeArtifact } from '@/store/officeArtifactStore'
import type { OfficeUpload } from '@/store/officeUploadStore'
import {
  getRevisionSnapshot, openRevision, pendingRevision, refreshRevision,
  rollbackRevision, submitRevision, subscribeRevision, openConversation, clearRevisionConversation,
  createRevisionConversation, listRevisionConversations, snapshotOutputs,
  type RevisionAction, type RevisionOutput,
} from '@/store/revisionStore'
import { qualityLabel } from './views/officeViewUtils'
import { AttachmentPicker } from './AttachmentPicker'
import { RevisionComparison } from './RevisionComparison'
import { RevisionPreviewFrame } from './RevisionPreviewFrame'
import './RevisionChatPanel.css'

export function RevisionChatPanel({ artifact, onClose }: { artifact?: OfficeArtifact; onClose: () => void }) {
  const { session, loading, error } = useSyncExternalStore(subscribeRevision, getRevisionSnapshot)
  const [draft, setDraft] = useState('')
  const [action, setAction] = useState<RevisionAction>('auto')
  const [mode, setMode] = useState('auto')
  const [selected, setSelected] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [localError, setLocalError] = useState('')
  const [pollError, setPollError] = useState('')
  const [showDiff, setShowDiff] = useState(false)
  const [targetKind, setTargetKind] = useState('docx')
  const [companionPdf, setCompanionPdf] = useState(false)
  const [uploads, setUploads] = useState<OfficeUpload[]>([])
  const [budget, setBudget] = useState('0')
  const [executionMode, setExecutionMode] = useState<'cloud' | 'local'>('cloud')
  const [outputSlot, setOutputSlot] = useState('')
  const [historicalOutputs, setHistoricalOutputs] = useState<RevisionOutput[]>([])
  const [baseOutputs, setBaseOutputs] = useState<RevisionOutput[]>([])
  const [conversations, setConversations] = useState<{ id: string; title: string }[]>([])
  const panel = useRef<HTMLDivElement>(null)
  const input = useRef<HTMLTextAreaElement>(null)
  const log = useRef<HTMLDivElement>(null)
  const pending = pendingRevision(session)
  const sessionId = session?.session_id
  const pendingId = pending?.id
  const busy = submitting || Boolean(pending)

  useEffect(() => { if (artifact) void openRevision(artifact); else void openConversation() }, [artifact])
  useEffect(() => {
    void listRevisionConversations().then((result) => setConversations(result.sessions)).catch(() => {})
  }, [session?.history.length])
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null
    panel.current?.focus()
    return () => { previousFocus?.focus() }
  }, [])
  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight, behavior: 'smooth' })
  }, [session?.history.length, pending?.id])
  useEffect(() => {
    if (!sessionId || !pendingId) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const activeSessionId = sessionId
    async function poll() {
      try {
        await refreshRevision(activeSessionId)
        if (!cancelled) setPollError('')
      } catch {
        if (!cancelled) setPollError('连接暂时中断，正在重连；请勿重复提交。')
      }
      if (!cancelled) timer = setTimeout(() => void poll(), 1800)
    }
    timer = setTimeout(() => void poll(), 1200)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [sessionId, pendingId])

  const anchor = session?.versions.find((item) => item.id === (selected || session.artifact_id)) ?? session?.artifact
  const anchorId = anchor?.id
  const previousId = anchor?.parent_id
  useEffect(() => {
    let cancelled = false
    if (anchorId) void snapshotOutputs(anchorId).then(({ outputs }) => { if (!cancelled) setHistoricalOutputs(outputs) }).catch(() => { if (!cancelled) setHistoricalOutputs([]) })
    if (previousId) void snapshotOutputs(previousId).then(({ outputs }) => { if (!cancelled) setBaseOutputs(outputs) }).catch(() => { if (!cancelled) setBaseOutputs([]) })
    return () => { cancelled = true }
  }, [anchorId, previousId])
  const displayedOutputs = !anchor ? [] : anchor.id === session?.artifact_id ? session?.outputs ?? [] : historicalOutputs
  const viewedOutput = displayedOutputs.find((item) => item.slot === outputSlot)
    ?? displayedOutputs.find((item) => item.artifact_id === anchor?.id) ?? displayedOutputs[0]
  const viewed = viewedOutput?.artifact ?? anchor
  const previous = (previousId ? baseOutputs.find((item) => item.slot === viewedOutput?.slot)?.artifact : null)
    ?? (previousId ? session?.versions.find((item) => item.id === previousId) : null)
  const current = session?.artifact
  const accepted = session?.versions.find((item) => item.id === session.accepted_artifact_id)

  async function send() {
    if (!draft.trim() || busy) return
    setLocalError('')
    setSubmitting(true)
    try {
      if (!session) await createRevisionConversation({ target_kinds: companionPdf && ['docx', 'pptx', 'xlsx'].includes(targetKind) ? [targetKind, 'pdf'] : [targetKind],
        upload_ids: uploads.map((upload) => upload.id), budget: Number(budget), execution_mode: executionMode })
      const currentOutput = session?.outputs?.find((item) => item.slot === outputSlot)
        ?? session?.outputs?.find((item) => item.artifact_id === session.artifact_id)
      if (await submitRevision(draft.trim(), action, mode, { target_kind: action === 'add' || action === 'convert' ? targetKind : undefined,
        artifact_id: currentOutput?.artifact_id })) { setDraft(''); setSelected('') }
    } catch (failure) {
      setLocalError(failure instanceof Error ? failure.message : String(failure))
    } finally { setSubmitting(false) }
  }
  async function restore() {
    if (!anchor || busy) return
    setSubmitting(true); setLocalError('')
    try { await rollbackRevision(anchor.id); setSelected('') }
    catch (failure) { setLocalError(failure instanceof Error ? failure.message : String(failure)) }
    finally { setSubmitting(false) }
  }

  return createPortal(
    <div className="revision-backdrop" onClick={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <div className="revision-panel" role="dialog" aria-modal="true" aria-labelledby="revision-title" tabIndex={-1}
        ref={panel} onKeyDown={(event) => {
          if (event.key === 'Escape') onClose()
          if (event.key === 'Tab') {
            const controls = panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], iframe')
            if (!controls?.length) return
            const first = controls[0], last = controls[controls.length - 1]
            if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { event.preventDefault(); last.focus() }
            else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.current)) { event.preventDefault(); first.focus() }
          }
        }}>
        <header className="revision-header">
          <div><span className="revision-eyebrow">持续编辑 · 保留每一版</span><h2 id="revision-title">对话修改</h2>
            <p>{artifact?.title ?? current?.title ?? '在这里提出需求，生成后继续修改'}</p></div>
          <button type="button" onClick={onClose} aria-label="关闭对话修改">关闭</button>
        </header>
        <div className="revision-conversation-switch"><label>会话<select value={session?.session_id ?? ''} disabled={busy}
          onChange={(event) => { setSelected(''); setOutputSlot(''); if (event.target.value) void openConversation(event.target.value); else clearRevisionConversation() }}>
          <option value="">新建生成对话</option>{conversations.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
        </select></label><button type="button" disabled={busy} onClick={() => { clearRevisionConversation(); setSelected(''); setAction('auto'); setDraft('') }}>新对话</button></div>
        <div className="revision-status">
          <span>当前编辑 <strong>v{current?.version ?? '—'}</strong></span>
          <span>最近验收通过 <strong>{accepted ? `v${accepted.version ?? 1}` : '暂无'}</strong></span>
          {current && <span>{qualityLabel(current)}</span>}
        </div>
        {loading ? <p role="status">正在恢复会话…</p> : null}
        {error || localError || pollError ? <p className="revision-error" role="alert">{error || localError || pollError}</p> : null}
        {!loading ? <>
          <nav className="revision-versions" aria-label="历史版本">
            {(session?.versions ?? []).map((version) => <button type="button" key={version.id}
              aria-pressed={viewed?.id === version.id} onClick={() => { setSelected(version.id); setShowDiff(false) }}>
              v{version.version ?? 1}{version.id === session?.artifact_id ? ' · 编辑中' : ''}
            </button>)}
          </nav>
          <div className="revision-body">
            <section className="revision-conversation">
              <div className="revision-log" ref={log} role="log" aria-label="修改记录" aria-live="polite">
                {!session?.history.length ? <div className="revision-welcome"><h3>{current ? '从这一版继续完善' : '从需求到成品，在同一段对话中完成'}</h3>
                  <p>描述要修改的内容。每次修改生成独立版本，旧文件保留；也可以先讨论方案。</p>
                  <p>支持生成、修改、新增配套文件和类型转换。源文件修改会沿关联关系更新配套内容及PDF；回滚会恢复整组文件。</p>
                  <p>代码修改会规划跨文件接口、依赖和测试，在独立目录生成新版本。每次结果都显示实际验收状态。</p>
                </div> : null}
                {(session?.history ?? []).map((entry) => <div key={entry.operation_id} className="revision-turn">
                  <div className="revision-user"><small>{{ discuss: '讨论', rollback: '回滚', generate: '首次生成', add: '新增配套', convert: '类型转换', revise: '修改要求', edit: '正文编辑' }[entry.action]}</small><p>{entry.feedback}</p></div>
                  <div className="revision-answer"><p>{entry.summary || '本次操作未完成'}</p>
                    {entry.error ? <p className="revision-error">{entry.error}</p> : null}
                    {entry.artifact_id ? <button type="button" onClick={() => setSelected(entry.artifact_id!)}>查看 v{entry.version} · {entry.quality_status === 'passed' ? '验收通过' : entry.quality_status === 'failed' ? '验收未通过' : '待验证'}</button> : null}
                  </div>
                </div>)}
                {pending ? <p className="revision-progress" role="status">正在{pending.status === 'queued' ? '准备' : '处理'}：{pending.payload.feedback}<br />关闭面板后任务仍会继续，重新打开可查看结果。</p> : null}
              </div>
              <form className="revision-composer" onSubmit={(event) => { event.preventDefault(); void send() }}>
                <div className="revision-controls">
                  {current ? <>
                  <label>操作<select value={action} onChange={(event) => setAction(event.target.value as typeof action)} disabled={busy}>
                    <option value="auto">按消息自动识别</option>
                    <option value="revise">执行修改</option><option value="discuss">仅讨论，不改文件</option>
                    <option value="add">新增配套产物</option><option value="convert">转换文件类型</option>
                  </select></label>
                  {action === 'revise' ? <label>方式<select value={mode} onChange={(event) => setMode(event.target.value)} disabled={busy}>
                    <option value="auto">自动判断</option><option value="targeted">定点修改</option><option value="rewrite">整体重写</option>
                  </select></label> : null}
                  </> : null}
                  {!current || action === 'add' || action === 'convert' ? <label>目标类型<select value={targetKind} onChange={(event) => setTargetKind(event.target.value)} disabled={busy || Boolean(session && !current)}>
                    <option value="docx">Word</option><option value="pptx">PPT</option><option value="xlsx">Excel</option>
                    <option value="pdf">PDF</option><option value="html">HTML</option><option value="code">代码项目</option>
                  </select></label> : null}
                </div>
                {!current && !session ? <div className="revision-initial-settings">
                  {['docx', 'pptx', 'xlsx'].includes(targetKind) ? <label><input type="checkbox" checked={companionPdf} onChange={(event) => setCompanionPdf(event.target.checked)} />同时生成并联动PDF</label> : null}
                  <label>预算上限（元，0为不限）<input type="number" min="0" step="0.1" value={budget} onChange={(event) => setBudget(event.target.value)} /></label>
                  <label>模型数据去向<select value={executionMode} onChange={(event) => setExecutionMode(event.target.value as typeof executionMode)}><option value="cloud">配置的模型API</option><option value="local">仅本机模型API</option></select></label>
                  <AttachmentPicker value={uploads} onChange={setUploads} removeFromStorage={false} />
                </div> : null}
                {selected && selected !== session?.artifact_id ? <p>正在预览历史版本；发送要求仍会修改当前编辑版 v{current?.version}。使用“恢复此版”可改变编辑起点。</p> : null}
                <label className="revision-input-label" htmlFor="revision-feedback">{action === 'discuss' ? '关于当前文件的问题' : '继续说需要改进的地方'}</label>
                <textarea id="revision-feedback" ref={input} value={draft} maxLength={6000} rows={3}
                  placeholder={current ? '例如：把第三章压缩一半，保留其他章节' : '描述要生成的内容、用途及验收要求'} disabled={busy}
                  onChange={(event) => setDraft(event.target.value)} />
                <div className="revision-send"><small>沿用原任务的模型、隐私模式与剩余预算；会产生模型用量。</small>
                  <button type="submit" disabled={busy || !draft.trim()}>{busy ? '处理中…' : !current ? '生成首版' : action === 'discuss' ? '发送问题' : action === 'add' ? '生成配套产物' : action === 'convert' ? '生成转换版本' : '生成修订版'}</button></div>
              </form>
            </section>
            <section className="revision-preview" aria-label="版本预览">
              <div className="revision-preview-header"><strong>预览 v{viewed?.version ?? 1}</strong>
                {previous ? <button type="button" onClick={() => setShowDiff(!showDiff)}>{showDiff ? '查看文件' : '完整版本对比'}</button> : null}
                {anchor && anchor.id !== session?.artifact_id ? <button type="button" disabled={busy} onClick={() => void restore()}>恢复整组快照</button> : null}
              </div>
                {displayedOutputs.length ? <label>产物<select value={viewedOutput?.slot ?? ''} onChange={(event) => { setOutputSlot(event.target.value); setShowDiff(false) }}>
                {displayedOutputs.map((item) => <option key={item.slot} value={item.slot}>{item.kind.toUpperCase()}{item.derived_from ? ' · 联动导出' : item.sync_from ? ' · 内容联动' : ''}</option>)}</select></label> : null}
              {showDiff && previous && viewed ? <RevisionComparison key={`${viewed.id}:${previous.id}`} artifactId={viewed.id} baseId={previous.id} />
                : viewed?.file_name && /\.(docx|pptx|xlsx|pdf|html)$/i.test(viewed.file_name) ?
                  <RevisionPreviewFrame key={viewed.id} title={`v${viewed.version ?? 1} 文件预览`} native={!/\.html$/i.test(viewed.file_name)} url={`${OFFICE_API_BASE}/api/artifacts/${encodeURIComponent(viewed.id)}/${/\.html$/i.test(viewed.file_name) ? 'code_preview' : 'preview'}`} />
                  : <pre>{viewed?.content}</pre>}
              <small>版本对比读取完整文件内容及可解析格式；页面外观可并排预览。</small>
              {viewed?.file_name ? <a href={`${OFFICE_API_BASE}/api/artifacts/${encodeURIComponent(viewed.id)}/download`} target="_blank" rel="noreferrer">下载此版本</a> : null}
            </section>
          </div>
        </> : null}
      </div>
    </div>, document.body,
  )
}
