import { Container, Graphics, Text } from 'pixi.js'

const BUBBLE_STYLE = {
  fontFamily: 'system-ui, -apple-system, sans-serif',
  fontSize: 11,
  fill: 0x333333,
  wordWrap: true,
  wordWrapWidth: 140,
  lineHeight: 15,
} as const

const THOUGHT_STYLE = {
  ...BUBBLE_STYLE,
  fill: 0x5d4a1f,
  wordWrapWidth: 170,
} as const

/** 气泡变体：talk 为白色对话泡；thought 为淡黄思考泡（决策依据展示） */
export type BubbleVariant = 'talk' | 'thought'

const VARIANT_COLORS: Record<BubbleVariant, { bg: number; border: number }> = {
  talk: { bg: 0xffffff, border: 0xe0e0e0 },
  thought: { bg: 0xfff6d6, border: 0xe8cf8a },
}

export class Bubble extends Container {
  private bg: Graphics
  private messageText: Text
  private lifetime = 0
  private variant: BubbleVariant = 'talk'

  constructor() {
    super()
    this.bg = new Graphics()
    this.messageText = new Text({ text: '', style: BUBBLE_STYLE })
    this.messageText.anchor.set(0.5, 0.5)
    this.addChild(this.bg, this.messageText)
    this.visible = false
  }

  show(text: string, duration = 4, variant: BubbleVariant = 'talk') {
    this.variant = variant
    this.messageText.style = variant === 'thought' ? THOUGHT_STYLE : BUBBLE_STYLE
    this.messageText.text = variant === 'thought' ? `💭 ${text}` : text
    this.lifetime = duration
    this.visible = true
    this.redraw()
  }

  hide() {
    this.visible = false
    this.lifetime = 0
  }

  update(dt: number): boolean {
    if (!this.visible) return false
    this.lifetime -= dt
    const alpha = Math.min(1, this.lifetime / 0.5)
    this.alpha = alpha
    if (this.lifetime <= 0) {
      this.hide()
      return false
    }
    return true
  }

  private redraw() {
    const padX = 10
    const padY = 7
    const w = this.messageText.width + padX * 2
    const h = this.messageText.height + padY * 2
    const r = 8
    const bodyTop = -h - 14
    const colors = VARIANT_COLORS[this.variant]

    this.bg.clear()
    this.bg.roundRect(-w / 2, bodyTop, w, h, r)
    this.bg.fill({ color: colors.bg, alpha: 0.96 })
    this.bg.stroke({ color: colors.border, width: 1 })

    // tail
    this.bg.moveTo(-6, -14)
    this.bg.lineTo(0, -4)
    this.bg.lineTo(6, -14)
    this.bg.fill({ color: colors.bg, alpha: 0.96 })

    // 背景在负 Y，文字中心需对齐圆角框中部（原点在尾巴处会漂到显示器上）
    this.messageText.position.set(0, bodyTop + h / 2)
  }

  /** 尾巴尖在容器内的 Y（与 redraw 里 lineTo(0, -4) 一致） */
  static readonly TAIL_TIP_Y = -4
}
