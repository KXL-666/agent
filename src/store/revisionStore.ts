import { OFFICE_API_BASE } from '@/config/officeMode'
import { upsertArtifact, type OfficeArtifact } from './officeArtifactStore'

export type RevisionEntry = {
  operation_id: string
  action: 'generate' | 'revise' | 'discuss' | 'rollback' | 'add' | 'convert' | 'edit'
  feedback: string
  summary: string
  status: string
  error?: string
  artifact_id?: string
  version?: number
  quality_status?: string
}
export type RevisionOperation = {
  id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'interrupted'
  payload: { feedback?: string }
}
export type RevisionSession = {
  session_id: string
  root_id: string
  artifact_id: string
  accepted_artifact_id: string | null
  artifact: OfficeArtifact | null
  history: RevisionEntry[]
  versions: OfficeArtifact[]
  operations: RevisionOperation[]
  outputs?: RevisionOutput[]
  settings?: ConversationSettings
}
export type RevisionOutput = { slot: string; artifact_id: string; derived_from: string | null; sync_from?: string | null; artifact: OfficeArtifact; kind: string }
export type ConversationSettings = { target_kinds: string[]; upload_ids: string[]; budget: number; execution_mode: 'cloud' | 'local' }
export type RevisionAction = 'auto' | 'revise' | 'discuss' | 'add' | 'convert'

export function pendingRevision(session: RevisionSession | null) {
  return session?.operations.find((op) => op.status === 'queued' || op.status === 'running')
}

class RequestError extends Error {
  status: number
  constructor(message: string, status: number) { super(message); this.status = status }
}
function requestId() {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`
}
async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${OFFICE_API_BASE}/api/revision${path}`, body === undefined ? undefined : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  const payload = await response.json()
  if (!response.ok) throw new RequestError(typeof payload.detail === 'string' ? payload.detail : '修订请求失败', response.status)
  return payload as T
}

let state: { session: RevisionSession | null; loading: boolean; error: string } = {
  session: null, loading: false, error: '',
}
let selection = 0
const listeners = new Set<() => void>()
function setState(patch: Partial<typeof state>) {
  state = { ...state, ...patch }
  listeners.forEach((listener) => listener())
}
function remember(session: RevisionSession) {
  try {
    localStorage.setItem(`office-revision:${session.root_id}`, session.session_id)
    localStorage.setItem('office-revision:last', session.session_id)
  } catch { /* optional cache */ }
}
export const subscribeRevision = (listener: () => void) => {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}
export const getRevisionSnapshot = () => state

export async function openRevision(artifact: OfficeArtifact, branch = false) {
  const turn = ++selection
  setState({ session: null, loading: true, error: '' })
  try {
    let session: RevisionSession | undefined
    if (!branch) {
      const { sessions } = await request<{ sessions: { id: string }[] }>(`/sessions?artifact_id=${encodeURIComponent(artifact.id)}`)
      if (sessions[0]) session = await request<RevisionSession>(`/session/${sessions[0].id}`)
    }
    session ??= await request<RevisionSession>('/session', { artifact_id: artifact.id })
    if (turn !== selection) return
    remember(session)
    setState({ session, loading: false })
  } catch (error) {
    if (turn === selection) setState({ loading: false, error: String(error instanceof Error ? error.message : error) })
  }
}

export async function openConversation(sessionId?: string) {
  const turn = ++selection
  setState({ session: null, loading: true, error: '' })
  try {
    let remembered = sessionId
    if (!remembered) { try { remembered = localStorage.getItem('office-revision:last') ?? undefined } catch { /* optional */ } }
    const session = remembered ? await request<RevisionSession>(`/session/${remembered}`) : null
    if (turn === selection) setState({ session, loading: false })
  } catch {
    if (turn === selection) setState({ session: null, loading: false })
  }
}

export function clearRevisionConversation() {
  ++selection
  setState({ session: null, loading: false, error: '' })
}

export async function createRevisionConversation(settings: ConversationSettings) {
  const session = await request<RevisionSession>('/session', settings)
  ++selection
  remember(session)
  setState({ session, loading: false, error: '' })
}

export async function listRevisionConversations() {
  return request<{ sessions: { id: string; title: string; updated_at: number }[] }>('/sessions')
}

export async function snapshotOutputs(artifactId: string) {
  return request<{ outputs: RevisionOutput[] }>(`/snapshot/${encodeURIComponent(artifactId)}`)
}

export type RevisionDiff = { complete: boolean; content_changes: number; format_changes: number; text_diff: string;
  changes: { location: string; type: string; content_changed: boolean; format_changed: boolean;
    before: { text: string; format: unknown } | null; after: { text: string; format: unknown } | null }[] }
export async function loadRevisionDiff(artifactId: string, baseId: string) {
  return request<RevisionDiff>(`/diff/${encodeURIComponent(artifactId)}?base_id=${encodeURIComponent(baseId)}`)
}
export type RevisionVisual = { status: 'ready' | 'unavailable'; render_id?: string; changed_pages?: number; reason?: string; note?: string;
  pages: { page: number; status: string; regions: number[][]; changed_fraction: number; width: number; height: number }[] }
export async function loadRevisionVisual(artifactId: string, baseId: string) {
  return request<RevisionVisual>(`/visual/${encodeURIComponent(artifactId)}?base_id=${encodeURIComponent(baseId)}`)
}

export async function refreshRevision(sessionId: string) {
  const session = await request<RevisionSession>(`/session/${sessionId}`)
  if (state.session?.session_id !== sessionId) return
  setState({ session, error: '' })
  session.versions.forEach(upsertArtifact)
  remember(session)
}

// Keep the same key after an ambiguous network failure. Retrying a response
// lost in transit retrieves that operation instead of paying for a second edit.
let retry: { signature: string; id: string; expectedId: string; action?: string } | null = null
export async function submitRevision(feedback: string, action: RevisionAction, mode: string,
                                     options?: { target_kind?: string; artifact_id?: string }) {
  const session = state.session
  if (!session || pendingRevision(session)) return false
  const effectiveAction = session.artifact_id ? action : 'generate'
  const signature = JSON.stringify([session.session_id, feedback, action, mode, options])
  if (retry?.signature !== signature) retry = { signature, id: requestId(), expectedId: session.artifact_id, action: effectiveAction }
  try {
    await request(`/session/${session.session_id}/message`, {
      feedback, action: retry.action, mode, ...options, request_id: retry.id, expected_artifact_id: retry.expectedId,
    })
    await refreshRevision(session.session_id)
    retry = null
    return true
  } catch (error) {
    if (error instanceof RequestError && error.status < 500) retry = null
    setState({ error: error instanceof Error ? error.message : String(error) })
    // A 409 may mean another tab has advanced the session. Fetch the truth.
    try { await refreshRevision(session.session_id) } catch { /* retry is retained */ }
    setState({ error: error instanceof Error ? error.message : String(error) })
    return false
  }
}

export async function rollbackRevision(targetId: string) {
  const session = state.session
  if (!session || pendingRevision(session)) return
  const signature = JSON.stringify([session.session_id, 'rollback', targetId])
  if (retry?.signature !== signature) retry = { signature, id: requestId(), expectedId: session.artifact_id }
  try {
    await request(`/session/${session.session_id}/rollback`, {
      target_id: targetId, request_id: retry.id, expected_artifact_id: retry.expectedId,
    })
    await refreshRevision(session.session_id)
    retry = null
  } catch (error) {
    if (error instanceof RequestError && error.status < 500) retry = null
    throw error
  }
}
