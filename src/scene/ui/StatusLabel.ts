import { Container, Graphics, Text } from 'pixi.js'

const NAME_STYLE = {
  fontFamily: 'system-ui, -apple-system, sans-serif',
  fontSize: 11,
  fontWeight: '600' as const,
  fill: 0x1f2937,
  // 透明背景：深色文字 + 白色柔光，压在浅色墙面或深色显示器上都读得清
  dropShadow: {
    color: 0xffffff,
    alpha: 0.95,
    blur: 3,
    distance: 0,
  },
}

const TASK_STYLE = {
  fontFamily: 'system-ui, -apple-system, sans-serif',
  fontSize: 10,
  fill: 0x666666,
  fontStyle: 'italic' as const,
}

const STATE_COLORS: Record<string, number> = {
  idle: 0xaaaaaa,
  walking: 0x4a90d9,
  working: 0x50b86c,
  talking: 0xe8a838,
  thinking: 0x9b6dd7,
}

/** 名字顶边与任务框底边的间距（任务在名字上方） */
const NAME_TASK_GAP = 2
/** 名字底边与发顶之间的空隙 */
const CROWN_GAP = 8

export class StatusLabel extends Container {
  private nameText: Text
  private taskText: Text
  private stateDot: Graphics
  private taskBg: Graphics
  private currentState = 'idle'
  private baseName: string
  private avatar = ''

  constructor(name: string) {
    super()
    this.baseName = name
    this.nameText = new Text({ text: name, style: NAME_STYLE })
    this.nameText.anchor.set(0.5, 1)
    this.nameText.position.set(0, 0)

    this.taskBg = new Graphics()
    this.taskText = new Text({ text: '', style: TASK_STYLE })
    this.taskText.anchor.set(0.5, 0)

    this.stateDot = new Graphics()
    this.addChild(this.taskBg, this.nameText, this.taskText, this.stateDot)
    this.taskBg.visible = false
    this.renderName()
  }

  setName(name: string) {
    this.baseName = name
    this.renderName()
  }

  /** 自定义头像 emoji：非空时叠在名牌名字前，空串恢复纯名字 */
  setAvatar(avatar: string) {
    if (avatar === this.avatar) return
    this.avatar = avatar
    this.renderName()
  }

  private renderName() {
    this.nameText.text = this.avatar ? `${this.avatar} ${this.baseName}` : this.baseName
    this.paintStateDot()
  }

  setTask(task?: string) {
    if (!task) {
      this.taskText.text = ''
      this.taskBg.visible = false
      return
    }
    this.taskText.text = task
    this.taskBg.visible = true

    this.layoutTaskAboveName()
  }

  private layoutTaskAboveName() {
    const padX = 5
    const padY = 3
    const w = this.taskText.width + padX * 2
    const h = this.taskText.height + padY * 2
    // 任务框叠在名字上方，两者不重叠
    const nameTop = -this.nameText.height
    const taskTop = nameTop - NAME_TASK_GAP - h

    this.taskBg.clear()
    this.taskBg.roundRect(-w / 2, taskTop, w, h, 3)
    this.taskBg.fill({ color: 0xffffff, alpha: 0.9 })
    this.taskText.position.set(0, taskTop + padY)
  }

  setState(state: string) {
    this.currentState = state
    this.paintStateDot()
  }

  /** 发顶 Y：名字贴在发顶上方，任务叠在名字上面 */
  layout(crownTopY: number) {
    this.position.set(0, crownTopY - CROWN_GAP)
  }

  /** 名字/任务标签块顶边 Y（Agent 本地坐标） */
  getLabelTopY(crownTopY: number): number {
    const baselineY = crownTopY - CROWN_GAP
    const nameBlockH = this.nameText.height
    if (!this.taskBg.visible) {
      return baselineY - nameBlockH
    }
    const taskBoxH = this.taskText.height + 6
    return baselineY - nameBlockH - NAME_TASK_GAP - taskBoxH
  }

  private paintStateDot() {
    const color = STATE_COLORS[this.currentState] ?? 0xaaaaaa
    this.stateDot.clear()
    this.stateDot.circle(
      this.nameText.width * 0.5 + 6,
      -this.nameText.height * 0.5,
      3.5,
    )
    this.stateDot.fill(color)
  }
}
