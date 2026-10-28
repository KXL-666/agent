/** 跨视图共享的常量、纯函数与类型定义
 *
 * 从 OfficeViews.tsx 抽取，避免各视图文件重复声明；
 * 这里仅放无状态/无副作用的纯逻辑，React 组件仍留在各自的视图文件里。
 */

import { OFFICE_API_BASE } from '@/config/officeMode'
import { showToast } from '@/store/officeUiStore'
import type { OfficeArtifact } from '@/store/officeArtifactStore'
import type { SprintTask } from '@/store/officeTaskStore'

/* ------------------------------- 通用小工具 ------------------------------- */

export const AGENT_STATE_LABELS: Record<string, string> = {
  idle: '空闲',
  walking: '走动中',
  working: '工作中',
  talking: '沟通中',
  thinking: '思考中',
}

export function hexColor(color: number): string {
  return `#${color.toString(16).padStart(6, '0')}`
}

export function timeLabel(ts: number): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

/* ------------------------------- 任务看板 ------------------------------- */

export const STATUS_ORDER = ['待分派', '待开发', '待测试', '待评审', '打回', '通过']

/* ------------------------------- 产物相关 ------------------------------- */

export const ARTIFACT_KIND_LABELS: Record<OfficeArtifact['kind'], string> = {
  prd: 'PRD',
  dev: '开发',
  test: '测试',
  review: '评审',
  summary: '总结',
  custom: '生成',
  final: '交付物',
}

/** 有真实文件时走后端下载，否则退回 Markdown Blob */
export function downloadArtifact(artifact: OfficeArtifact) {
  if (artifact.file_name) {
    const anchor = document.createElement('a')
    anchor.href = `${OFFICE_API_BASE}/api/artifacts/${artifact.id}/download`
    anchor.download = artifact.file_name.split(/[\\/]/).pop() || artifact.title
    anchor.click()
    showToast('真实文件已开始下载')
    return
  }
  const blob = new Blob([artifact.content], {
    type: 'text/markdown;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${artifact.title}.md`
  anchor.click()
  URL.revokeObjectURL(url)
  showToast('产物已下载')
}

/** 从 file_name 取扩展名用于徽标展示 */
export function fileExt(artifact: OfficeArtifact): string | null {
  if (!artifact.file_name) return null
  const dot = artifact.file_name.lastIndexOf('.')
  return dot >= 0 ? artifact.file_name.slice(dot) : null
}

export function sourceLabel(artifact: OfficeArtifact, legacyDemo: boolean): string {
  const source = (!artifact.source || artifact.source === 'unknown') && legacyDemo ? 'demo' : artifact.source
  switch (source) {
    case 'demo': return '脚本演示'
    case 'model': return '真实模型'
    case 'fallback': return '兜底模板'
    case 'local': return '本地处理'
    default: return '来源未记录'
  }
}

export function qualityLabel(artifact: OfficeArtifact): string {
  switch (artifact.quality_status) {
    case 'passed': return '自动验收通过'
    case 'failed': return '存在验收问题'
    case 'unchecked': return '部分项目待验证'
    default: return '暂无验收记录'
  }
}

export function canPreviewFile(artifact: OfficeArtifact): boolean {
  return /\.(docx|pptx|xlsx|pdf|html)$/i.test(artifact.file_name ?? '')
}

/* ------------------------------- 通知中心 ------------------------------- */

export const KIND_LABELS: Record<string, string> = {
  sprint_start: 'Sprint',
  sprint_done: 'Sprint',
  sprint_error: '异常',
  desk_visit: '沟通',
  desk_visit_tour: '会议',
  task_update: '任务',
  artifact_new: '产物',
  instruction_added: '指示',
  prd: '需求',
  dev: '开发',
  test: '测试',
  review: '评审',
  summary: '复盘',
  final: '成品',
  custom: '成品',
}

/* ------------------------------ 版本对比 diff ------------------------------ */

/** 简单行级 diff：基于 LCS，把两份文本对比为 same/add/del 行序列（内容已裁剪，行数有限） */
export type DiffLine = { type: 'same' | 'add' | 'del'; text: string }

export function diffLines(oldText: string, newText: string): DiffLine[] {
  const a = oldText.split('\n')
  const b = newText.split('\n')
  const m = a.length
  const n = b.length
  const dp: number[][] = Array.from({ length: m + 1 }, () =>
    new Array<number>(n + 1).fill(0),
  )
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] =
        a[i] === b[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      out.push({ type: 'same', text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ type: 'del', text: a[i] })
      i++
    } else {
      out.push({ type: 'add', text: b[j] })
      j++
    }
  }
  while (i < m) out.push({ type: 'del', text: a[i++] })
  while (j < n) out.push({ type: 'add', text: b[j++] })
  return out
}

/* ------------------------------ 文件中心 payload ------------------------------ */

export type FilesPayload = {
  requirement: string
  running: boolean
  updated_at: number
  tasks: SprintTask[]
  artifacts: OfficeArtifact[]
  report_md: string
}
