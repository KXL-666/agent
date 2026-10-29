import { useEffect } from 'react'

/** 图片点击放大预览：点击遮罩或按 Esc 关闭 */
export function ImagePreviewOverlay({
  src,
  alt,
  onClose,
}: {
  src: string
  alt: string
  onClose: () => void
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="image-preview-overlay" onClick={onClose}>
      <img src={src} alt={alt} onClick={(event) => event.stopPropagation()} />
      <button type="button" aria-label="关闭预览" title="关闭（Esc）" onClick={onClose}>
        ×
      </button>
    </div>
  )
}
