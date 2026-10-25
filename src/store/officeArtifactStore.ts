/** 产物（PRD/开发说明/测试报告/评审记录/总结）的轻量响应式 store */

export type OfficeArtifact = {
  id: string
  kind: 'prd' | 'dev' | 'test' | 'review' | 'summary' | 'custom' | 'final'
  title: string
  author_roster: number | null
  task_id: string | null
  content: string
  /** 关联的真实文件名（backend/deliverables/ 下），为 null 时下载 Markdown */
  file_name?: string | null
  created_at: number
  /** 内容来自兜底/Mock 或真实文件生成失败：前端显示警示徽标 */
  degraded?: boolean
  degraded_reason?: string
  /** 内容来源与成品质检分别记录，避免把兜底内容误当作质检失败 */
  source?: 'demo' | 'model' | 'fallback' | 'local' | 'unknown'
  quality_status?: 'passed' | 'failed' | 'unchecked' | 'unknown'
  /** 修订来源与原版本号（产物版本链） */
  parent_id?: string | null
  version?: number
  sprint_id?: string
}

let items: OfficeArtifact[] = []

const listeners = new Set<() => void>()

function notify() {
  for (const listener of listeners) listener()
}

export function subscribeArtifacts(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getArtifactsSnapshot(): OfficeArtifact[] {
  return items
}

/** 全量替换（文件中心拉取时用） */
export function setArtifacts(list: OfficeArtifact[]) {
  items = [...list].sort((a, b) => a.id.localeCompare(b.id))
  notify()
}

/** WS 单条推送：已存在则更新，否则追加 */
export function upsertArtifact(artifact: OfficeArtifact) {
  const index = items.findIndex((item) => item.id === artifact.id)
  if (index >= 0) {
    items = items.map((item) => (item.id === artifact.id ? artifact : item))
  } else {
    items = [...items, artifact].sort((a, b) => a.id.localeCompare(b.id))
  }
  notify()
}

/** 新 Sprint 开始时清空 */
export function clearArtifacts() {
  if (items.length === 0) return
  items = []
  notify()
}

/** 删除单条产物（后端删除成功后同步前端） */
export function removeArtifact(artifactId: string) {
  const next = items.filter((item) => item.id !== artifactId)
  if (next.length === items.length) return
  items = next
  notify()
}

/** 产物中心当前查看的 Sprint（空 = 当前进行中的 Sprint） */
let viewedSprintId = ''

export function setViewedSprint(sprintId: string) {
  viewedSprintId = sprintId
  notify()
}

export function getViewedSprint(): string {
  return viewedSprintId
}


