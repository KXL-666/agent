import assert from 'node:assert/strict'
import { test } from 'node:test'
import { isLoopbackOrigin, isSameOriginRequest } from '../scripts/browser-origin.mjs'

test('local services reject foreign, opaque and spoofed browser origins', () => {
  for (const value of ['http://localhost:5173', 'https://127.0.0.1:8000', 'http://[::1]:5173']) {
    assert.equal(isLoopbackOrigin(value), true, value)
  }
  for (const value of ['null', 'https://evil.example', 'http://localhost.evil.example', 'http://evil@localhost', 'http://localhost/path']) {
    assert.equal(isLoopbackOrigin(value), false, value)
  }
})

test('password-protected LAN proxy checks the original origin before rewriting', () => {
  const host = '192.168.1.10:5174'
  assert.equal(isSameOriginRequest({ headers: { host, origin: `http://${host}` } }), true)
  assert.equal(isSameOriginRequest({ headers: { host } }), true)
  assert.equal(isSameOriginRequest({ headers: { host, origin: 'https://evil.example' } }), false)
  assert.equal(isSameOriginRequest({ headers: { host, origin: 'null' } }), false)
})
