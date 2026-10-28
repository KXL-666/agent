import { useEffect, useState } from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { loadRevisionDiff, loadRevisionVisual, type RevisionDiff, type RevisionVisual } from '@/store/revisionStore'
import { RevisionPreviewFrame } from './RevisionPreviewFrame'

export function RevisionComparison({ artifactId, baseId }: { artifactId: string; baseId: string }) {
  const [result, setResult] = useState<RevisionDiff | null>(null)
  const [error, setError] = useState('')
  const [visual, setVisual] = useState(false)
  const [pages, setPages] = useState<RevisionVisual | null>(null)
  const [pageIndex, setPageIndex] = useState(0)
  useEffect(() => {
    let cancelled = false
    void loadRevisionDiff(artifactId, baseId).then((diff) => {
      if (!cancelled) { setResult(diff); setError('') }
    }).catch((failure) => { if (!cancelled) setError(String(failure)) })
    return () => { cancelled = true }
  }, [artifactId, baseId])
  useEffect(() => {
    if (!visual) return
    let cancelled = false
    void loadRevisionVisual(artifactId, baseId).then((response) => {
      if (!cancelled) setPages(response)
    }).catch((failure) => { if (!cancelled) setPages({ status: 'unavailable', reason: String(failure), pages: [] }) })
    return () => { cancelled = true }
  }, [visual, artifactId, baseId])
  function download() {
    if (!result) return
    const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a'); link.href = url; link.download = '完整版本差异.json'; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  return <div className="revision-comparison">
    <div className="revision-compare-tools"><button type="button" onClick={() => setVisual(!visual)}>{visual ? '内容与格式差异' : '并排预览页面'}</button>
      {result ? <button type="button" onClick={download}>下载完整差异</button> : null}</div>
    {error ? <p role="alert">{error}</p> : !result ? <p role="status">读取完整文件并比较…</p> : <>
      <p>{result.content_changes} 处内容变化 · {result.format_changes} 处原生格式变化</p>
      {visual ? <>
        {!pages ? <p role="status">正在读取实际页面并计算变化区域…</p> : pages.status === 'ready' ? <>
          <div className="revision-compare-tools"><label>页面<select value={pageIndex} onChange={(event) => setPageIndex(Number(event.target.value))}>
            {pages.pages.map((page, index) => <option key={page.page} value={index}>第{page.page}页 · {page.status === 'unchanged' ? '无变化' : page.status === 'added' ? '新增' : page.status === 'removed' ? '删除' : `${page.regions.length}个变化区域`}</option>)}
          </select></label><span>{pages.changed_pages}页发生变化</span></div>
          <p>{pages.note}</p>
          {pages.pages[pageIndex] ? <div className="revision-visual-regions">
            {(['before', 'after'] as const).map((side) => <figure key={side}><figcaption>{side === 'before' ? '修改前' : '修改后'}</figcaption>
              <img alt={`${side === 'before' ? '修改前' : '修改后'}第${pages.pages[pageIndex].page}页，红框标注变化区域`} src={`${OFFICE_API_BASE}/api/revision/visual/${encodeURIComponent(artifactId)}/${encodeURIComponent(baseId)}/${pages.render_id}/pages/${pages.pages[pageIndex].page}/${side}`} />
            </figure>)}
          </div> : null}
        </> : <p role="status">页面变化标注不可用：{pages.reason}。下方保留原始并排预览。</p>}
        {pages?.status === 'unavailable' ? <div className="revision-visual-diff">
          <RevisionPreviewFrame title="修改前页面" url={`${OFFICE_API_BASE}/api/artifacts/${encodeURIComponent(baseId)}/preview`} />
          <RevisionPreviewFrame title="修改后页面" url={`${OFFICE_API_BASE}/api/artifacts/${encodeURIComponent(artifactId)}/preview`} />
        </div> : null}
      </> : <>
        <pre className="revision-diff">{result.text_diff || '完整文本未变化'}</pre>
        {result.changes.filter((change) => change.format_changed).map((change, index) => <details key={index}>
          <summary>{change.location} · 格式变化</summary>
          <div className="revision-format-diff"><pre>{JSON.stringify(change.before?.format, null, 2)}</pre><pre>{JSON.stringify(change.after?.format, null, 2)}</pre></div>
        </details>)}
      </>}
    </>}
  </div>
}
