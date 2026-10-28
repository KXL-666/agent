import { useEffect, useState } from 'react'

// Sandboxed documents have an opaque origin. Browsers can block their local
// network requests even when the parent application's API calls succeed.
// Render server-produced page images in the parent; keep HTML scripts sandboxed.
export function RevisionPreviewFrame({ title, url, native = true }: { title: string; url: string; native?: boolean }) {
  const [preview, setPreview] = useState<{ html: string; pages: string[] } | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const abort = new AbortController()
    void fetch(url, { signal: abort.signal }).then(async (response) => {
      if (!response.ok) throw new Error(`文件预览加载失败（${response.status}）`)
      const html = await response.text()
      if (abort.signal.aborted) return
      const document = new DOMParser().parseFromString(html, 'text/html')
      const pages = native ? Array.from(document.querySelectorAll('img')).map((image) => image.getAttribute('src') ?? '')
        .filter((source) => /^\/api\/artifacts\/[^/]+\/quality\/pages\/\d+$/.test(source))
        .map((source) => new URL(source, url).href) : []
      setPreview({ html, pages })
    }).catch((failure) => {
      if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure))
    })
    return () => abort.abort()
  }, [url, native])
  if (error) return <p role="alert">{error}</p>
  if (!preview) return <p role="status">正在加载文件预览…</p>
  return preview.pages.length ? <div className="revision-document-pages" aria-label={title}>
    {preview.pages.map((page, index) => <img key={page} src={page} alt={`${title}，第${index + 1}页`} loading="lazy" />)}
  </div> : <iframe title={title} sandbox="" srcDoc={preview.html} />
}
