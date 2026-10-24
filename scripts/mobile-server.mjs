/** Password-protected LAN access to the built app and local services. */
import { createServer, request as httpRequest } from 'node:http'
import { connect } from 'node:net'
import { randomBytes, timingSafeEqual } from 'node:crypto'
import { readFile, stat } from 'node:fs/promises'
import { extname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(fileURLToPath(new URL('../dist/', import.meta.url)))
const password = process.env.OFFICE_MOBILE_PASSWORD ?? ''
const port = Number(process.env.OFFICE_MOBILE_PORT ?? 5174)
const session = randomBytes(32).toString('hex')
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.json': 'application/json' }

if (password.length < 12) {
  console.error('请设置至少 12 位的 OFFICE_MOBILE_PASSWORD，再启动手机访问服务。')
  process.exit(1)
}

function equal(left, right) {
  const a = Buffer.from(left)
  const b = Buffer.from(right)
  return a.length === b.length && timingSafeEqual(a, b)
}

function authenticated(req) {
  if ((req.headers.cookie ?? '').split(';').some(value => equal(value.trim(), `office_mobile=${session}`))) return true
  const auth = req.headers.authorization ?? ''
  if (!auth.startsWith('Basic ')) return false
  let credentials = ''
  try { credentials = Buffer.from(auth.slice(6), 'base64').toString('utf8') } catch { return false }
  return equal(credentials, `office:${password}`)
}

function backendPort(path) {
  if (path.startsWith('/api/') || path === '/ws/office') return 8000
  if (path === '/actions') return 8765
  return null
}

const server = createServer(async (req, res) => {
  if (!authenticated(req)) {
    res.writeHead(401, { 'WWW-Authenticate': 'Basic realm="AI Office mobile"' })
    res.end('需要访问密码')
    return
  }
  res.setHeader('Set-Cookie', `office_mobile=${session}; HttpOnly; SameSite=Strict; Path=/`)
  res.setHeader('Cache-Control', 'no-store')
  const parsed = new URL(req.url ?? '/', 'http://localhost')
  const path = parsed.pathname
  const targetPort = backendPort(path)
  if (targetPort) {
    const headers = { ...req.headers, host: `127.0.0.1:${targetPort}` }
    delete headers.authorization
    delete headers.cookie
    const proxy = httpRequest({ hostname: '127.0.0.1', port: targetPort, path: path + parsed.search, method: req.method, headers }, upstream => {
      res.writeHead(upstream.statusCode ?? 502, upstream.headers)
      upstream.pipe(res)
    })
    proxy.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end('本机服务未启动') })
    req.pipe(proxy)
    return
  }
  if (req.method !== 'GET' && req.method !== 'HEAD') { res.writeHead(405); res.end(); return }
  try {
    const relative = decodeURIComponent(path).replace(/^\/+/, '') || 'index.html'
    const file = resolve(root, relative)
    if (file !== root && !file.startsWith(root + sep)) { res.writeHead(403); res.end(); return }
    const info = await stat(file)
    if (!info.isFile()) { res.writeHead(404); res.end(); return }
    const body = await readFile(file)
    res.writeHead(200, { 'Content-Type': mime[extname(file)] ?? 'application/octet-stream' })
    res.end(req.method === 'HEAD' ? undefined : body)
  } catch { res.writeHead(404); res.end('页面不存在；请先运行 npm run build') }
})

server.on('upgrade', (req, socket, head) => {
  if (!authenticated(req) || new URL(req.url ?? '/', 'http://localhost').pathname !== '/ws/office') {
    socket.end('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n')
    return
  }
  const upstream = connect(8000, '127.0.0.1')
  upstream.on('connect', () => {
    const headers = { ...req.headers, host: '127.0.0.1:8000' }
    delete headers.authorization
    delete headers.cookie
    const lines = [`${req.method} ${req.url} HTTP/1.1`, ...Object.entries(headers).map(([key, value]) => `${key}: ${value}`), '', '']
    upstream.write(lines.join('\r\n'))
    if (head.length) upstream.write(head)
    socket.pipe(upstream).pipe(socket)
  })
  upstream.on('error', () => socket.destroy())
  socket.on('error', () => upstream.destroy())
})

server.listen(port, '0.0.0.0', () => console.log(`手机访问服务已启动，端口 ${port}；仅在可信局域网使用。`))
