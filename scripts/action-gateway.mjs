import { createServer } from 'node:http'
import { isLoopbackOrigin } from './browser-origin.mjs'

const port = Number(process.env.OFFICE_ACTION_GATEWAY_PORT ?? 8765)
const actions = []

function sendJson(res, status, data) {
  const body = JSON.stringify(data)
  res.writeHead(status, {
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type,Accept',
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  })
  res.end(body)
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = ''
    req.setEncoding('utf8')
    req.on('data', (chunk) => {
      body += chunk
      if (body.length > 1024 * 1024) {
        reject(new Error('Request body too large'))
        req.destroy()
      }
    })
    req.on('end', () => resolve(body))
    req.on('error', reject)
  })
}

function normalizeActions(payload) {
  if (Array.isArray(payload)) return payload
  if (Array.isArray(payload?.actions)) return payload.actions
  return [payload]
}

const server = createServer(async (req, res) => {
  const origin = req.headers.origin
  res.setHeader('Vary', 'Origin')
  if (origin !== undefined && !isLoopbackOrigin(origin)) {
    sendJson(res, 403, { error: 'Browser origin is not allowed' })
    return
  }
  if (origin) res.setHeader('Access-Control-Allow-Origin', origin)
  const url = new URL(req.url ?? '/', `http://${req.headers.host ?? 'localhost'}`)

  if (req.method === 'OPTIONS') {
    sendJson(res, 204, {})
    return
  }

  if (req.method === 'GET' && url.pathname === '/health') {
    sendJson(res, 200, { ok: true, queued: actions.length })
    return
  }

  if (url.pathname !== '/actions') {
    sendJson(res, 404, { error: 'Not found' })
    return
  }

  if (req.method === 'GET') {
    const batch = actions.splice(0)
    sendJson(res, 200, { actions: batch })
    return
  }

  if (req.method === 'POST') {
    try {
      const body = await readBody(req)
      const payload = body.trim() ? JSON.parse(body) : null
      const batch = normalizeActions(payload)
      actions.push(...batch)
      sendJson(res, 202, { accepted: batch.length, queued: actions.length })
    } catch (error) {
      sendJson(res, 400, {
        error: error instanceof Error ? error.message : 'Invalid request body',
      })
    }
    return
  }

  sendJson(res, 405, { error: 'Method not allowed' })
})

server.listen(port, '127.0.0.1', () => {
  console.log(`[OfficeActionGateway] listening on http://localhost:${server.address().port}`)
  console.log('[OfficeActionGateway] POST /actions to enqueue, GET /actions to drain')
})

function shutdown() {
  server.close(() => process.exit(0))
}

process.on('SIGINT', shutdown)
process.on('SIGTERM', shutdown)
