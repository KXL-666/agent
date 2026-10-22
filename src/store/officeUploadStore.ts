/** 上传附件（文件/文件夹）的轻量响应式 store + 后端交互 */

import { OFFICE_API_BASE } from '../config/officeMode'

export type OfficeUpload = {
  id: string
  name: string
  size: number
  uploaded_at: number
}

let items: OfficeUpload[] = []

const listeners = new Set<() => void>()

function notify() {
  for (const listener of listeners) listener()
}

export function subscribeUploads(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getUploadsSnapshot(): OfficeUpload[] {
  return items
}

/** 初始拉取已上传列表 */
export async function fetchUploads(): Promise<void> {
  try {
    const res = await fetch(`${OFFICE_API_BASE}/api/uploads`)
    if (!res.ok) return
    const payload = (await res.json()) as { uploads?: OfficeUpload[] }
    items = payload.uploads ?? []
    notify()
  } catch {
    // 后端未启动时保持现状
  }
}

/** 上传一批文件（文件夹上传时浏览器会拆成多个 File），返回成功与超限跳过列表 */
export async function uploadFiles(
  files: File[],
): Promise<{ uploads: OfficeUpload[]; skipped: string[] }> {
  if (files.length === 0) return { uploads: [], skipped: [] }
  const form = new FormData()
  for (const file of files) form.append('files', file)
  const res = await fetch(`${OFFICE_API_BASE}/api/uploads`, {
    method: 'POST',
    body: form,
  })
  if (!res.ok) throw new Error(`上传失败：${res.status}`)
  const payload = (await res.json()) as {
    uploads: OfficeUpload[]
    skipped_too_large?: string[]
  }
  items = [...items, ...payload.uploads]
  notify()
  return { uploads: payload.uploads, skipped: payload.skipped_too_large ?? [] }
}

export async function removeUpload(id: string): Promise<void> {
  try {
    await fetch(`${OFFICE_API_BASE}/api/uploads/${id}`, { method: 'DELETE' })
  } finally {
    items = items.filter((item) => item.id !== id)
    notify()
  }
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
