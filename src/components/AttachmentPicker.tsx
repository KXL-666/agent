/** 附件选择器：添加文件/文件夹，上传后以 chips 展示，供 Sprint/生成功能引用 */

import { useRef, useState, type ChangeEvent } from 'react'
import {
  formatSize,
  removeUpload,
  uploadFiles,
  type OfficeUpload,
} from '../store/officeUploadStore'
import { showToast } from '../store/officeUiStore'

type Props = {
  value: OfficeUpload[]
  onChange: (items: OfficeUpload[]) => void
}

export function AttachmentPicker({ value, onChange }: Props) {
  const fileRef = useRef<HTMLInputElement>(null)
  const dirRef = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)

  async function handlePick(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? [])
    event.target.value = '' // 允许重复选同一文件
    if (files.length === 0) return
    setUploading(true)
    try {
      const result = await uploadFiles(files)
      onChange([...value, ...result.uploads])
      if (result.skipped.length > 0) {
        showToast(
          `以下文件超过 50MB 未上传：${result.skipped.join('、')}（请压缩或拆分后重试）`,
        )
      }
    } catch (error) {
      showToast(error instanceof Error ? error.message : '附件上传失败，请确认后端已启动')
    } finally {
      setUploading(false)
    }
  }

  async function handleRemove(id: string) {
    onChange(value.filter((item) => item.id !== id))
    await removeUpload(id)
  }

  return (
    <div className="attachment-picker">
      <div className="attachment-actions">
        <button
          type="button"
          disabled={uploading}
          onClick={() => fileRef.current?.click()}
        >
          📎 添加文件
        </button>
        <button
          type="button"
          disabled={uploading}
          onClick={() => dirRef.current?.click()}
        >
          📁 添加文件夹
        </button>
        {uploading ? <span className="attachment-hint">上传中…</span> : null}
      </div>
      <input
        ref={fileRef}
        type="file"
        multiple
        hidden
        onChange={handlePick}
      />
      <input
        ref={dirRef}
        type="file"
        hidden
        // @ts-expect-error 非标准属性，浏览器用于文件夹上传
        webkitdirectory=""
        onChange={handlePick}
      />
      {value.length > 0 ? (
        <ul className="attachment-chips">
          {value.map((item) => (
            <li key={item.id} className="attachment-chip">
              <span className="attachment-name" title={item.name}>
                {item.name}
              </span>
              <em>{formatSize(item.size)}</em>
              <button
                type="button"
                aria-label="移除附件"
                onClick={() => handleRemove(item.id)}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
