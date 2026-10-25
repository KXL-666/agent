import type { Desk } from '@/types/agent'
/** 与 DeskEntity 一致 */
export const DESK_DISPLAY_WIDTH = (152 * 2) / 3

export const NAV_AGENT_CLEARANCE = 16

/** 侧面入席点须落在桌面水平范围外 */
export const SIDE_OFFSET =
  Math.ceil(DESK_DISPLAY_WIDTH / 2) + NAV_AGENT_CLEARANCE

/** 列过道在桌面外侧 */
export const COL_AISLE_MARGIN = SIDE_OFFSET + 6

/** 行向过道与座椅同高：离座/回座/寻路均不先上抬再过道 */
export function getRowCorridorY(desk: Desk): number {
  return desk.seatY
}
