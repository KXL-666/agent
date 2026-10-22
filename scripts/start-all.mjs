/**
 * 一键启动：后端 FastAPI(8000) + 动作网关 + 前端 Vite(5173)
 * 用法：npm run start
 * 前提：已安装 Python 依赖（见 README）与 npm 依赖
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const backendDir = join(root, 'backend')

function findPython() {
  if (process.env.PYTHON) return { command: process.env.PYTHON, args: [] }
  for (const candidate of [
    { command: 'python', args: [] },
    { command: 'py', args: ['-3'] },
  ]) {
    const probe = spawnSync(candidate.command, [...candidate.args, '--version'], {
      encoding: 'utf8',
    })
    if (!probe.error && probe.status === 0) return candidate
  }
  return null
}

const python = findPython()
if (!python) {
  console.error('[start] 未找到 Python，请先安装 Python 3.10+ 并加入 PATH')
  process.exit(1)
}

for (const file of ['main.py', 'requirements.txt']) {
  if (!existsSync(join(backendDir, file))) {
    console.error(`[start] 缺少 backend/${file}`)
    process.exit(1)
  }
}

const children = []

function start(name, command, args, options = {}) {
  const child = spawn(command, args, { stdio: 'inherit', ...options })
  children.push(child)
  child.on('exit', (code) => {
    if (code && code !== 0) {
      console.error(`[start] ${name} 退出，code=${code}`)
      shutdown(code)
    }
  })
  return child
}

function shutdown(code = 0) {
  for (const child of children) {
    if (!child.killed) child.kill('SIGTERM')
  }
  process.exit(code)
}

process.on('SIGINT', () => shutdown(0))
process.on('SIGTERM', () => shutdown(0))

start('backend', python.command, [
  ...python.args,
  '-m',
  'uvicorn',
  'main:app',
  '--host',
  '127.0.0.1',
  '--port',
  '8000',
], { cwd: backendDir })
start('action-gateway', process.execPath, [join(root, 'scripts/action-gateway.mjs')])
start('vite', 'vite', [], { cwd: root, shell: true })

console.log('[start] 前端 http://localhost:5173  后端 http://127.0.0.1:8000')
