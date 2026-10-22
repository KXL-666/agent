import { AGENT_ROSTER } from '@/scene/layout/officeLayout'

/** Spine Chibi Stickers 可用皮肤（9 套，16 人会轮换复用） */
export const CHIBI_CHARACTER_SKINS = [
  'misaki',
  'erikari',
  'nate',
  'harri',
  'luke',
  'soeren',
  'mario',
  'sinisa',
  'spineboy',
] as const

export type ChibiSkinId = (typeof CHIBI_CHARACTER_SKINS)[number]

/** 皮肤中文显示名（配置弹窗形象选择器用） */
export const CHIBI_SKIN_LABELS: Record<ChibiSkinId, string> = {
  misaki: '美咲',
  erikari: '绘里香',
  nate: '内特',
  harri: '哈里',
  luke: '卢克',
  soeren: '索伦',
  mario: '马里奥',
  sinisa: '西尼沙',
  spineboy: '斯派恩男孩',
}

const SKIN_SET = new Set<string>(CHIBI_CHARACTER_SKINS)

const SKIN_BY_ID = new Map(
  AGENT_ROSTER.map((a, i) => [
    a.id,
    CHIBI_CHARACTER_SKINS[i % CHIBI_CHARACTER_SKINS.length],
  ]),
)

/** 名册默认皮肤（未自定义时的分配结果） */
export function getChibiSkinName(agentId: string): string {
  return SKIN_BY_ID.get(agentId) ?? 'spineboy'
}

/** 实际生效皮肤：自定义皮肤合法时优先，否则回落名册默认 */
export function resolveChibiSkin(agentId: string, customSkin?: string): string {
  if (customSkin && SKIN_SET.has(customSkin)) return customSkin
  return getChibiSkinName(agentId)
}
