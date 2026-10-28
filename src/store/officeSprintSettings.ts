import { OFFICE_API_BASE } from '@/config/officeMode'

/** 新建任务的默认设置：侧栏齿轮面板维护，对话页直接发送时使用；本地持久化 */
export type SprintSettings = {
  taskKind: 'auto' | 'file' | 'software'
  executionStyle: 'collaborative' | 'quick'
  executionMode: 'cloud' | 'local'
  saveDir: string
  customDir: string
  fast: boolean
  budgetText: string
}

const STORAGE_KEY = 'office-sprint-settings-v1'

const DEFAULTS: SprintSettings = {
  taskKind: 'auto',
  executionStyle: 'collaborative',
  executionMode: 'cloud',
  saveDir: '',
  customDir: '',
  fast: false,
  budgetText: '',
}

function load(): SprintSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { ...DEFAULTS }
    return { ...DEFAULTS, ...(JSON.parse(raw) as Partial<SprintSettings>) }
  } catch {
    return { ...DEFAULTS }
  }
}

let settings: SprintSettings = load()
const listeners = new Set<() => void>()

export function subscribeSprintSettings(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getSprintSettingsSnapshot(): SprintSettings {
  return settings
}

export function updateSprintSettings(patch: Partial<SprintSettings>): void {
  settings = { ...settings, ...patch }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings))
  } catch {
    // 隐私模式下写入失败仅影响本次会话
  }
  listeners.forEach((listener) => listener())
}

/** 启动 Sprint：返回 null 表示成功，否则为错误信息 */
export async function startSprint(
  requirement: string,
  uploadIds: string[],
  fast = false,
  saveDir = '',
  budget = 0,
  taskKind = 'auto',
  executionMode = 'cloud',
  executionStyle = 'collaborative',
): Promise<string | null> {
  try {
    const res = await fetch(`${OFFICE_API_BASE}/api/sprint/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requirement, upload_ids: uploadIds, fast, save_dir: saveDir, budget, task_kind: taskKind, execution_mode: executionMode, execution_style: executionStyle }),
    })
    if (res.ok) return null
    const detail = await res.json().catch(() => null)
    return typeof detail?.detail === 'string' ? detail.detail : '启动失败'
  } catch {
    return '无法连接后端，请确认服务已启动'
  }
}
