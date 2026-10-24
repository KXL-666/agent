import { useEffect, useState } from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { AttachmentPicker } from './AttachmentPicker'
import { fetchUploads, getUploadsSnapshot, subscribeUploads, type OfficeUpload } from '@/store/officeUploadStore'
import { useSyncExternalStore } from 'react'

type Source = { upload_id: string; name: string; location: string; content: string }

export function UploadedKnowledge() {
  const uploaded = useSyncExternalStore(subscribeUploads, getUploadsSnapshot)
  const [selected, setSelected] = useState<OfficeUpload[]>([])
  const [query, setQuery] = useState('')
  const [sources, setSources] = useState<Source[]>([])
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => { void fetchUploads() }, [])

  async function search() {
    if (!query.trim()) return
    setBusy(true)
    try {
      const params = new URLSearchParams({ q: query.trim() })
      if (selected.length) params.set('ids', selected.map(item => item.id).join(','))
      const response = await fetch(`${OFFICE_API_BASE}/api/uploads/search?${params}`)
      if (!response.ok) throw new Error('检索失败')
      const result = await response.json() as { answer: string; sources: Source[] }
      setMessage(result.answer)
      setSources(result.sources)
    } catch {
      setMessage('检索失败，请确认后端已启动')
      setSources([])
    } finally {
      setBusy(false)
    }
  }

  return <section className="view-card knowledge-panel">
    <h2>我的资料库</h2>
    <p className="view-card-meta">文件在本机建立分段索引；搜索只返回原文片段，不调用模型。图片与扫描版 PDF 目前不会产生可搜索文字。</p>
    <AttachmentPicker value={selected} onChange={setSelected} removeFromStorage={false} />
    <p className="view-card-meta">已上传 {uploaded.length} 个文件。勾选可限定搜索范围；未勾选时搜索全部已上传文件。</p>
    {uploaded.length ? <div className="knowledge-file-list" aria-label="已上传文件">{uploaded.map(item => <label key={item.id}>
      <input type="checkbox" checked={selected.some(value => value.id === item.id)} onChange={event => setSelected(event.target.checked ? [...selected.filter(value => value.id !== item.id), item] : selected.filter(value => value.id !== item.id))} />
      <span>{item.name}</span>
    </label>)}</div> : null}
    <div className="knowledge-search-row">
      <input aria-label="搜索文件内容" value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void search() }} placeholder="例如：验收标准、预算、项目背景" />
      <button type="button" disabled={busy || !query.trim()} onClick={() => void search()}>{busy ? '查找中…' : '查找原文'}</button>
    </div>
    {message ? <p role="status">{message}</p> : null}
    {sources.length ? <ol className="knowledge-results">{sources.map((source, index) => <li key={`${source.upload_id}-${source.location}-${index}`}>
      <strong>{source.name} · {source.location}</strong>
      <p>{source.content}</p>
    </li>)}</ol> : null}
  </section>
}
