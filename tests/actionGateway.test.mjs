import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

test('foreign browser requests cannot enqueue or consume local actions', async (t) => {
  const child = spawn(process.execPath, [fileURLToPath(new URL('../scripts/action-gateway.mjs', import.meta.url))], {
    env: { ...process.env, OFFICE_ACTION_GATEWAY_PORT: '0' }, stdio: ['ignore', 'pipe', 'pipe'],
  })
  t.after(() => { if (child.exitCode === null) child.kill() })
  const base = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Gateway startup timed out')), 5000)
    let output = ''
    child.once('error', error => { clearTimeout(timer); reject(error) })
    child.once('exit', code => { clearTimeout(timer); reject(new Error(`Gateway exited: ${code}`)) })
    child.stdout.on('data', chunk => {
      output += String(chunk)
      const match = output.match(/listening on http:\/\/localhost:(\d+)/)
      if (match) { clearTimeout(timer); resolve(`http://127.0.0.1:${match[1]}`) }
    })
  })
  let response = await fetch(`${base}/actions`, { method: 'POST', headers: { Origin: 'https://evil.example', 'Content-Type': 'application/json' }, body: '{"type":"test"}' })
  assert.equal(response.status, 403)
  response = await fetch(`${base}/actions`)
  assert.deepEqual(await response.json(), { actions: [] })
  response = await fetch(`${base}/actions`, { method: 'POST', headers: { Origin: 'http://localhost:5173', 'Content-Type': 'application/json' }, body: '{"type":"test"}' })
  assert.equal(response.status, 202)
  assert.equal(response.headers.get('access-control-allow-origin'), 'http://localhost:5173')
  response = await fetch(`${base}/actions`, { headers: { Origin: 'null' } })
  assert.equal(response.status, 403)
  response = await fetch(`${base}/actions`)
  assert.deepEqual(await response.json(), { actions: [{ type: 'test' }] })
})
