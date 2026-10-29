import assert from 'node:assert/strict'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

test('修订请求响应丢失后复用同一请求与原始版本，切换会话不被迟到结果覆盖', async () => {
  const originalFetch = globalThis.fetch
  const artifact = { id: 'A1', kind: 'final', title: '通知', version: 1, content: '旧版', created_at: 1 }
  let session = { session_id: 'S1', root_id: 'A1', artifact_id: 'A1', artifact,
    accepted_artifact_id: 'A1', history: [], versions: [artifact], operations: [] }
  const requests = []
  let loseFirstResponse = true
  let resolveOldSnapshot
  let delayOldSnapshot = false
  const response = (value) => ({ ok: true, status: 200, json: async () => value })
  globalThis.fetch = async (url, options) => {
    if (url.includes('/sessions?')) return response({ sessions: [{ id: url.includes('A9') ? 'S9' : 'S1' }] })
    if (url.endsWith('/session/S9')) return response({ ...session, session_id: 'S9', root_id: 'A9' })
    if (url.endsWith('/message')) {
      const body = JSON.parse(options.body)
      requests.push(body)
      if (loseFirstResponse) {
        loseFirstResponse = false
        session = { ...session, artifact_id: 'A2', artifact: { ...artifact, id: 'A2', version: 2 } }
        throw new TypeError('response lost after server completed the edit')
      }
      return response({ operation: { id: 'OP1', status: 'completed' } })
    }
    if (delayOldSnapshot) return new Promise((resolve) => { resolveOldSnapshot = () => resolve(response(session)) })
    return response(session)
  }
  const server = await createServer({ configFile: false,
    root: fileURLToPath(new URL('..', import.meta.url)),
    resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
    server: { middlewareMode: true, hmr: false, ws: false, watch: null }, appType: 'custom',
  })
  try {
    const store = await server.ssrLoadModule('/src/store/revisionStore.ts')
    await store.openRevision(artifact)
    assert.equal(await store.submitRevision('修改时间', 'revise', 'auto'), false)
    assert.equal(store.getRevisionSnapshot().session.artifact_id, 'A2')
    assert.equal(await store.submitRevision('修改时间', 'revise', 'auto'), true)
    assert.equal(requests[0].request_id, requests[1].request_id)
    assert.equal(requests[1].expected_artifact_id, 'A1')
    delayOldSnapshot = true
    const oldRefresh = store.refreshRevision('S1')
    await store.openRevision({ ...artifact, id: 'A9' })
    resolveOldSnapshot()
    await oldRefresh
    assert.equal(store.getRevisionSnapshot().session.session_id, 'S9')
    // The first generation may finish before its response is lost too. Its
    // retry must stay a generation request with the original empty pointer.
    delayOldSnapshot = false
    session = { ...session, session_id: 'NEW', artifact_id: '', artifact: null, versions: [] }
    await store.createRevisionConversation({ target_kinds: ['docx'], upload_ids: [], budget: 0, execution_mode: 'cloud' })
    loseFirstResponse = true
    assert.equal(await store.submitRevision('生成通知', 'revise', 'auto'), false)
    assert.equal(await store.submitRevision('生成通知', 'revise', 'auto'), true)
    const generationRequests = requests.slice(-2)
    assert.equal(generationRequests[0].request_id, generationRequests[1].request_id)
    assert.equal(generationRequests[1].expected_artifact_id, '')
    assert.equal(generationRequests[1].action, 'generate')
  } finally {
    await server.close()
    globalThis.fetch = originalFetch
  }
})
