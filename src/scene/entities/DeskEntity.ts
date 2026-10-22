import { Container, Graphics, Sprite } from 'pixi.js'
import type { Desk } from '@/types/agent'
import { SEAT_OFFSET_Y } from '@/scene/layout/officeLayout'
import {
  getOfficeChairTexture,
  getOfficeDeskTexture,
} from '@/scene/assets/loadOfficeAssets'
import {
  computeChairLayerZ,
  computeDeskLayerZ,
} from '@/scene/systems/deskDepthSort'

const STYLE = {
  shadow: { color: 0x3d4f6e, alpha: 0.1 },
  deskTop: 0xfaf8f4,
  deskEdge: 0xe8e0d4,
  deskStroke: 0xd8d0c4,
  chairDark: 0x556b7d,
  chair: 0x7a8fa3,
  chairWheel: 0x3d4a56,
  keyboard: 0xeeedea,
  keyboardStroke: 0xd0ccc4,
  mouse: 0xf5f4f1,
} as const

const DESK_BASE_WIDTH = 152
const CHAIR_BASE_WIDTH = 104
const DESK_ANCHOR_Y = 0.62
const DESK_TARGET_WIDTH = (DESK_BASE_WIDTH * 2) / 3
const CHAIR_ANCHOR_Y = 0.36
const CHAIR_TARGET_WIDTH = CHAIR_BASE_WIDTH / 2

const CHAIR_DEPTH_AHEAD = 2

/** 屏幕画面：IDE 风格配色 */
const SCREEN = {
  bezel: 0x1b1e24,
  bg: 0x141b2e,
  titleBar: 0x2b3348,
  dot1: 0xf07178,
  dot2: 0xffcb6b,
  dot3: 0x8ae29a,
  codePalette: [0x7ec8ff, 0xc792ea, 0x8ae29a, 0xffcb6b, 0xf07178],
} as const

export class DeskEntity {
  readonly deskId: string
  readonly shadowGfx = new Graphics()
  readonly deskLayer = new Container()
  readonly chairLayer = new Container()
  readonly occupiedIndicator = new Graphics()

  private desk: Desk

  constructor(desk: Desk) {
    this.deskId = desk.id
    this.desk = desk

    for (const part of [
      this.shadowGfx,
      this.deskLayer,
      this.chairLayer,
      this.occupiedIndicator,
    ]) {
      part.position.set(desk.x, desk.y)
    }

    this.drawShadow()
    this.mountSprites()
  }

  /** 素材晚到或 HMR 后可重新挂载 PNG */
  remountSprites() {
    this.deskLayer.removeChildren()
    this.chairLayer.removeChildren()
    this.mountSprites()
  }

  updateDepthZ(agentPositions: { x: number; y: number }[]) {
    const deskZ = computeDeskLayerZ(this.desk, agentPositions)
    const chairZ = computeChairLayerZ(
      this.desk,
      agentPositions,
      CHAIR_DEPTH_AHEAD,
    )
    this.deskLayer.zIndex = deskZ
    this.shadowGfx.zIndex = deskZ - 0.5
    this.chairLayer.zIndex = chairZ
    this.occupiedIndicator.zIndex = chairZ + 0.5
  }

  setOccupied(occupied: boolean) {
    this.occupiedIndicator.clear()
    if (occupied) {
      this.occupiedIndicator.circle(0, SEAT_OFFSET_Y - 4, 5.5)
      this.occupiedIndicator.fill({ color: 0x50b86c, alpha: 0.85 })
      this.occupiedIndicator.stroke({ color: 0xffffff, width: 1.5, alpha: 0.6 })
    }
  }

  getSeatPosition() {
    return { x: this.desk.seatX, y: this.desk.seatY }
  }

  private mountSprites() {
    const deskTex = getOfficeDeskTexture()
    const chairTex = getOfficeChairTexture()

    if (deskTex) {
      const desk = new Sprite(deskTex)
      desk.anchor.set(0.5, DESK_ANCHOR_Y)
      desk.position.set(0, SEAT_OFFSET_Y - 14)
      desk.scale.set(DESK_TARGET_WIDTH / deskTex.width)
      this.deskLayer.addChild(desk)
      this.deskLayer.addChild(this.buildScreenOverlay())
    } else {
      this.drawDeskFallback()
    }

    if (chairTex) {
      const chair = new Sprite(chairTex)
      chair.anchor.set(0.5, CHAIR_ANCHOR_Y)
      chair.position.set(0, SEAT_OFFSET_Y)
      chair.scale.set(CHAIR_TARGET_WIDTH / chairTex.width)
      this.chairLayer.addChild(chair)
    } else {
      this.drawChairFallback()
    }
  }

  /**
   * 更大且有内容的显示器：覆盖 PNG 素材自带的小黑屏
   * 中心约在 desk 本地 (0, 0) 附近，尺寸 48×30 明显大于原屏幕
   */
  private buildScreenOverlay(): Graphics {
    const g = new Graphics()
    this.drawMonitorWithContent(g, 0, -2, 48, 30)
    return g
  }

  /**
   * 统一绘制「显示器 + IDE 画面」：外框 + 标题栏 + 代码行
   * (cx, cy) 为中心坐标，w/h 为含边框的整体尺寸
   */
  private drawMonitorWithContent(g: Graphics, cx: number, cy: number, w: number, h: number) {
    const bw = 3 // 边框厚度
    const sw = w - bw * 2 // 屏幕宽
    const sh = h - bw * 2 // 屏幕高
    const sx = cx - sw / 2
    const sy = cy - sh / 2

    // 1. 外框（深色金属边框，底部略厚暗示底座）
    g.roundRect(cx - w / 2, cy - h / 2, w, h, 3)
    g.fill(SCREEN.bezel)

    // 2. 屏幕底色
    g.roundRect(sx, sy, sw, sh, 2)
    g.fill(SCREEN.bg)

    // 3. 顶部标题栏（浅灰蓝）
    const barH = Math.max(3, Math.floor(sh * 0.22))
    g.rect(sx, sy, sw, barH)
    g.fill(SCREEN.titleBar)

    // 4. 窗口控制圆点（红、黄、绿）
    const dotR = Math.max(1.2, barH * 0.18)
    const dotX0 = sx + dotR * 2.5
    const dotY = sy + barH / 2
    g.circle(dotX0, dotY, dotR)
    g.fill(SCREEN.dot1)
    g.circle(dotX0 + dotR * 3, dotY, dotR)
    g.fill(SCREEN.dot2)
    g.circle(dotX0 + dotR * 6, dotY, dotR)
    g.fill(SCREEN.dot3)

    // 5. 代码行：模拟语法高亮，宽度随机递减
    const rowGap = Math.max(2.2, sh * 0.085)
    const rowY0 = sy + barH + rowGap * 0.8
    const rows = Math.floor((sh - barH - rowGap) / rowGap)
    for (let i = 0; i < rows; i++) {
      const y = rowY0 + i * rowGap
      const maxW = sw * (0.88 - (i % 3) * 0.08) // 每行长度递减，模拟缩进
      const segments = (i % 2) + 1
      let xOff = sx + sw * 0.06
      for (let s = 0; s < segments; s++) {
        const segW = maxW / (segments + 0.5) * (0.8 + (i * 0.13))
        const segH = Math.max(1.2, rowGap * 0.42)
        g.roundRect(xOff, y - segH / 2, Math.max(2, segW), segH, 0.8)
        g.fill(SCREEN.codePalette[(i + s) % SCREEN.codePalette.length])
        xOff += segW + sw * 0.04
      }
    }

    // 6. 底部指示条（光标闪烁暗示感）
    const cursorW = sw * 0.18
    const cursorH = Math.max(1.2, rowGap * 0.4)
    g.roundRect(sx + sw * 0.82 - cursorW, sy + sh - cursorH * 1.4, cursorW, cursorH, 0.6)
    g.fill(0x5c6370)
  }

  private drawShadow() {
    const g = this.shadowGfx
    g.clear()
    g.ellipse(0, SEAT_OFFSET_Y + 20, 54, 14)
    g.fill(STYLE.shadow)
  }

  private drawChairFallback() {
    const g = new Graphics()
    const seatY = 30
    const backTop = 40
    const backBottom = 58
    const baseY = 64

    g.ellipse(0, baseY, 30, 11)
    g.fill(STYLE.chairDark)
    for (let i = 0; i < 5; i++) {
      const a = (i / 5) * Math.PI * 2 - Math.PI / 2
      g.circle(Math.cos(a) * 24, baseY + Math.sin(a) * 6, 3.5)
      g.fill(STYLE.chairWheel)
    }
    g.roundRect(-24, backTop, 48, backBottom - backTop, 14)
    g.fill(STYLE.chair)
    g.roundRect(-22, seatY, 44, 14, 8)
    g.fill(STYLE.chair)

    g.position.set(0, SEAT_OFFSET_Y - 36)
    this.chairLayer.addChild(g)
  }

  private drawDeskFallback() {
    const g = new Graphics()

    g.roundRect(-46, -6, 92, 34, 10)
    g.fill(STYLE.deskTop)
    g.stroke({ color: STYLE.deskStroke, width: 1.5, alpha: 0.55 })
    g.roundRect(-44, 22, 88, 8, 4)
    g.fill(STYLE.deskEdge)

    // 显示器：放大版，含 IDE 画面
    this.drawMonitorWithContent(g, 0, -38, 48, 30)

    // 键盘 + 鼠标：随屏幕放大微调位置避免重叠
    g.roundRect(-22, 2, 44, 8, 4)
    g.fill(STYLE.keyboard)
    g.roundRect(-22, 2, 44, 8, 4)
    g.stroke({ color: STYLE.keyboardStroke, width: 0.8, alpha: 0.4 })

    g.ellipse(22, 8, 5, 7)
    g.fill(STYLE.mouse)

    this.deskLayer.addChild(g)
  }
}
