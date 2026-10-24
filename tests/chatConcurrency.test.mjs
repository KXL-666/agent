import assert from 'node:assert/strict'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

test('成员对话可并行、独立中断，交错回复不会串入其他线程', async () => {
  const memory = new Map()
  globalThis.localStorage = { getItem: key => memory.get(key), setItem: (key, value) => memory.set(key, value) }
  globalThis.window = { setTimeout }
  const server = await createServer({
    configFile: false, root: fileURLToPath(new URL('..', import.meta.url)),
    server: { middlewareMode: true, hmr: false, watch: null }, appType: 'custom',
  })
  try {
    const chat = await server.ssrLoadModule('/src/store/officeChatStore.ts')
    const first = chat.beginChat(1)
    const second = chat.beginChat(2)
    assert.deepEqual(chat.getPendingRosters(), [1, 2])
    assert.equal(chat.beginChat(1), null)
    chat.appendUserMessage(1, '产品问题')
    chat.appendUserMessage(2, '技术问题')
    chat.updateLastAssistantMessage(2, '技术回复', 'llm')
    chat.updateLastAssistantMessage(1, '产品回复', 'llm')
    chat.updateLastAssistantMessage(2, '技术回复完整内容', 'llm')
    assert.equal(chat.getThreadSnapshot(1).at(-1).content, '产品回复')
    assert.equal(chat.getThreadSnapshot(2).at(-1).content, '技术回复完整内容')
    chat.abortChat(1)
    assert.equal(first.signal.aborted, true)
    assert.equal(second.signal.aborted, false)
    chat.finishChat(1)
    assert.deepEqual(chat.getPendingRosters(), [2])
    chat.finishChat(2)
    assert.deepEqual(chat.getPendingRosters(), [])
  } finally {
    await server.close()
    delete globalThis.localStorage
    delete globalThis.window
  }
})
