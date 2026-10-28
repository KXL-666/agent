import { OFFICE_API_BASE } from '@/config/officeMode'
import { getArtifactTarget, getConversationId, type ChatMessage } from '@/store/officeChatStore'
import type { OfficeArtifact } from '@/store/officeArtifactStore'

export async function runConversationTurn(options: {
  thread: number
  message: string
  history: ChatMessage[]
  uploadIds?: string[]
  sprintId?: string
  signal?: AbortSignal
}): Promise<{ handled: boolean; intent?: 'chat' | 'requirement'; requirement?: string; reply?: string; artifacts?: OfficeArtifact[]; context?: string }> {
  const res = await fetch(`${OFFICE_API_BASE}/api/chat/turn`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    signal: options.signal,
    body: JSON.stringify({
      mode: options.thread === 0 ? 'talk' : 'solo',
      roster: options.thread || 1,
      session_id: getConversationId(options.thread),
      artifact_id: getArtifactTarget(options.thread),
      message: options.message,
      sprint_id: options.sprintId ?? '',
      history: options.history.filter(m => !m.hint).map(m => ({ role: m.role === 'task' ? 'assistant' : m.role, content: m.content })),
      upload_ids: options.uploadIds ?? [],
    }),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail || '本轮操作失败，请重试')
  return data
}
