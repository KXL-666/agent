/** 成员思考流的轻量响应式 store：记录各成员的决策依据，供任务详情与时间线回溯 */

export type OfficeThought = {
  id: string
  rosterNo: number
  thought: string
  phase: string
  taskId?: string
  ts: number
}

const THOUGHT_LIMIT = 120

let items: OfficeThought[] = []

const listeners = new Set<() => void>()

function notify() {
  for (const listener of listeners) listener()
}

export function subscribeThoughts(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getThoughtsSnapshot(): OfficeThought[] {
  return items
}

export function addThought(entry: Omit<OfficeThought, 'id' | 'ts'>) {
  if (!entry.thought.trim()) return
  items = [
    ...items,
    {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      ts: Date.now(),
      ...entry,
    },
  ].slice(-THOUGHT_LIMIT)
  notify()
}

/** 任务详情「决策依据」：该任务直接关联的思考 + 负责人在本轮的其他思考 */
export function thoughtsForTask(taskId: string, rosterNo?: number | null): OfficeThought[] {
  return items.filter(
    (item) =>
      item.taskId === taskId ||
      (rosterNo != null && item.rosterNo === rosterNo && !item.taskId),
  )
}

export function clearThoughts() {
  items = []
  notify()
}
