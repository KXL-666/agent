import { useState } from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'
import type { OfficeArtifact } from '@/store/officeArtifactStore'
import { setArtifactTarget } from '@/store/officeChatStore'

export function ConversationArtifact({ artifact, thread }: { artifact: OfficeArtifact; thread: number }) {
  const [open, setOpen] = useState(false)
  const base = `${OFFICE_API_BASE}/api/artifacts/${encodeURIComponent(artifact.id)}`
  function downloadText() {
    const url = URL.createObjectURL(new Blob([artifact.content], { type: 'text/markdown;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `${artifact.title}.md`
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  return (
    <div className="conversation-artifact">
      <strong>{artifact.title} · v{artifact.version ?? 1}</strong>
      <small>{artifact.id}{artifact.degraded ? ' · 生成或检查未通过' : ''}</small>
      <div className="conversation-artifact-actions">
        <button type="button" className="view-ghost-btn" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? '收起预览' : '预览'}</button>
        {artifact.file_name ? <a className="view-ghost-btn" href={`${base}/download`} download>下载</a> : <button type="button" className="view-ghost-btn" onClick={downloadText}>下载</button>}
        <button type="button" className="view-ghost-btn" onClick={() => setArtifactTarget(thread, artifact.id, `${artifact.title} · v${artifact.version ?? 1}`)}>继续修改此版本</button>
      </div>
      {open ? artifact.file_name ? <iframe title={`${artifact.title} v${artifact.version ?? 1}`} src={`${base}/preview`} sandbox="" /> : <pre>{artifact.content}</pre> : null}
    </div>
  )
}
