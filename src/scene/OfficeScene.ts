import { Application, Container, Graphics, Sprite, Texture } from 'pixi.js'
import type { FederatedPointerEvent } from 'pixi.js'
import type { Agent, AgentState } from '@/types/agent'
import { OFFICE_AUTO_DEMO } from '@/config/officeMode'
import {
  AGENT_ROSTER,
  COLORS,
  DESKS,
  pickHandoffVisitMessage,
  SCENE_HEIGHT,
  SCENE_WIDTH,
} from '@/scene/layout/officeLayout'
import { AgentEntity } from '@/scene/entities/AgentEntity'
import { DeskEntity } from '@/scene/entities/DeskEntity'
import {
  MovementSystem,
  isHomeDeskSeat,
} from '@/scene/systems/MovementSystem'
import { AnimationSystem } from '@/scene/systems/AnimationSystem'
import { OfficeSimulator } from '@/scene/simulation/OfficeSimulator'
import { startReturnToDesk } from '@/scene/simulation/deskVisit'
import { bindOfficeScene } from '@/scene/officeSceneBridge'
import {
  hasPendingVisitQueue,
  notifyVisitMissionActivity,
} from '@/services/officeActionDispatcher'
import { getOfficeAgents, setOfficeAgents } from '@/store/officeStore'
import {
  getOfficeBackgroundTexture,
  loadOfficeAssets,
} from '@/scene/assets/loadOfficeAssets'
import { loadSpineAssets } from '@/scene/assets/loadSpineAssets'

export type OfficeAgentClick = {
  agent: Agent
  rosterNo: number
  clientX: number
  clientY: number
}

type AutoWorkflowStep = {
  visitor: number
  host: number
  message: string
}

const AUTO_WORKFLOW_MAX_ACTIVE = 2
const AUTO_WORKFLOW_INTERVAL = 1.2

// ---------------------------------------------------------------------------
// 夜晚模式：地板/背景换深色 + 覆盖式夜色压暗 + add 混合灯光光晕（随暗色主题切换）
// ---------------------------------------------------------------------------

/** 世界地板夜间基色（压暗前） */
const NIGHT_BG_COLOR = 0x23262b
/** 夜色压暗层：覆盖房间+地板整幅画布（normal 混合），统一压暗避免 multiply 色差与多层黑边 */
const NIGHT_OVERLAY_COLOR = 0x0d1120
const NIGHT_OVERLAY_ALPHA = 0.48
/** 地板被压暗后的呈现色（0x23262b 叠加 0.48 压暗层）：渲染器背景与之保持一致，消除接缝色差 */
const NIGHT_FLOOR_DIMMED = 0x1a1d27

let glowTexture: Texture | null = null

/** 懒生成径向渐变光晕贴图（柔和类物理衰减），所有灯光共用一张 */
function getGlowTexture(): Texture {
  if (glowTexture) return glowTexture
  const size = 256
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = size
  const ctx = canvas.getContext('2d')!
  const gradient = ctx.createRadialGradient(
    size / 2, size / 2, 0,
    size / 2, size / 2, size / 2,
  )
  gradient.addColorStop(0, 'rgba(255,255,255,0.7)')
  gradient.addColorStop(0.3, 'rgba(255,255,255,0.36)')
  gradient.addColorStop(0.65, 'rgba(255,255,255,0.12)')
  gradient.addColorStop(1, 'rgba(255,255,255,0)')
  ctx.fillStyle = gradient
  ctx.fillRect(0, 0, size, size)
  glowTexture = Texture.from(canvas)
  return glowTexture
}

type NightLight = {
  sprite: Sprite
  baseAlpha: number
  phase: number
  speed: number
}

const AUTO_WORKFLOW_STEPS: AutoWorkflowStep[] = [
  { visitor: 1, host: 2, message: '新的任务来了，先帮我扫一下信息源。' },
  { visitor: 2, host: 3, message: '资料已经找到，麻烦整理一下。' },
  { visitor: 3, host: 4, message: '这份可以进入撰写了。' },
  { visitor: 4, host: 6, message: '初稿好了，请帮忙审一下。' },
  { visitor: 6, host: 4, message: '这里需要改一版，我标了重点。' },
  { visitor: 5, host: 1, message: '市场简报已经打包，请看一下。' },
  { visitor: 1, host: 5, message: '同步一下调研方向，准备发给团队。' },
  { visitor: 3, host: 6, message: '归档完成，合规队列可以接手。' },
  { visitor: 2, host: 5, message: '这些来源适合市场情报线。' },
  { visitor: 4, host: 1, message: '修改稿已完成，等你签批。' },
]

export class OfficeScene {
  private app: Application | null = null
  private world: Container | null = null
  private agentEntities = new Map<string, AgentEntity>()
  private deskEntities = new Map<string, DeskEntity>()
  private officeLayer: Container | null = null

  private movement = new MovementSystem()
  private animation = new AnimationSystem()
  private simulator = new OfficeSimulator()

  private agents: Agent[] = getOfficeAgents().map((a) => ({ ...a }))
  private paused = false
  private pausedAgentStates = new Map<string, { state: AgentState; currentTask?: string }>()
  private autoWorkflowTimer = 0.8
  private autoWorkflowIndex = 0
  private readonly options: {
    onAgentClick?: (event: OfficeAgentClick) => void
  }

  /** 用户缩放：在 fitStage 等比适配的基础上再乘 1×~2×，超出容器裁切 + 任意方向拖动平移 */
  private userZoom = 1
  private containerW = 0
  private containerH = 0
  private baseScale = 1
  private panDrag: {
    pointerId: number
    startX: number
    startY: number
    worldX: number
    worldY: number
    moved: boolean
  } | null = null
  /** 拖动平移刚结束的一小段时间内抑制小人点击，避免拖拽后误弹动作菜单 */
  private lastPanEndAt = 0
  private zoomListener: ((zoom: number) => void) | null = null

  /** 夜晚模式：灯光图层与呼吸动画状态 */
  private nightLayer: Container | null = null
  private nightLights: NightLight[] = []
  private nightMode = false
  private nightTime = 0
  /** 世界地板：暗色主题下直接换成与办公室周围一致的深色 */
  private floorGraphics: Graphics | null = null

  /** 切换夜晚/白天：地板换色 + 灯光图层显隐，渲染器背景同步为压暗后地板色 */
  setNightMode(on: boolean) {
    this.nightMode = on
    if (this.app) {
      this.app.renderer.background.color = on ? NIGHT_FLOOR_DIMMED : COLORS.floor
    }
    // 世界地板：夜晚 = 与办公室周围同色；白天 = 原色
    if (this.floorGraphics) {
      this.floorGraphics
        .clear()
        .rect(0, 0, SCENE_WIDTH, SCENE_HEIGHT)
        .fill(on ? NIGHT_BG_COLOR : COLORS.floor)
    }
    if (on) this.ensureNightLayer()
    if (this.nightLayer) this.nightLayer.visible = on
  }

  /** 首次开夜灯时构建图层：覆盖式夜色压暗层（铺满整幅画布）+ 各灯位 add 光晕 */
  private ensureNightLayer() {
    if (this.nightLayer || !this.world) return
    const layer = new Container()
    layer.label = 'night'
    layer.eventMode = 'none' // 纯视觉效果，不拦截拖拽平移与小人点击
    layer.visible = this.nightMode

    // 夜色压暗层：normal 混合半透明深蓝黑铺满整个场景画布（房间+地板+图片透明边距），
    // 统一压暗避免 multiply 在纯白房间上产生的色差与多层黑边
    const overlay = new Graphics()
    overlay
      .rect(0, 0, SCENE_WIDTH, SCENE_HEIGHT)
      .fill({ color: NIGHT_OVERLAY_COLOR, alpha: NIGHT_OVERLAY_ALPHA })
    layer.addChild(overlay)

    // 灯光光晕：add 混合在压暗层之上点亮灯位周围
    const texture = getGlowTexture()
    const addGlow = (
      x: number,
      y: number,
      radius: number,
      tint: number,
      alpha: number,
      speed: number,
    ) => {
      const glow = new Sprite(texture)
      glow.anchor.set(0.5)
      glow.position.set(x, y)
      glow.width = glow.height = radius * 2
      glow.tint = tint
      glow.alpha = alpha
      glow.blendMode = 'add'
      layer.addChild(glow)
      this.nightLights.push({
        sprite: glow,
        baseAlpha: alpha,
        phase: Math.random() * Math.PI * 2,
        speed,
      })
    }

    for (const desk of DESKS) {
      addGlow(desk.seatX, desk.seatY - 40, 165, 0xffc182, 0.55, 1.1) // 工位台灯暖光（柔和光池）
      addGlow(desk.seatX, desk.seatY - 12, 32, 0x9fd4ff, 0.34, 2.0) // 显示器冷光
    }
    addGlow(SCENE_WIDTH / 2, 60, 540, 0xffd9a0, 0.24, 0.7) // 顶部环境暖光洗墙
    addGlow(SCENE_WIDTH - 40, SCENE_HEIGHT / 2, 400, 0x8fb4ff, 0.24, 0.5) // 右侧窗月光

    // 挂在 world 最顶层：跟随缩放/平移，压暗与灯光覆盖地图与小人气泡
    this.world.addChild(layer)
    this.nightLayer = layer
  }

  constructor(options: { onAgentClick?: (event: OfficeAgentClick) => void } = {}) {
    this.options = options
  }

  async init(container: HTMLElement, width: number, height: number) {
    const app = new Application()
    await app.init({
      width,
      height,
      backgroundColor: COLORS.floor,
      antialias: true,
      resolution: window.devicePixelRatio || 1,
      autoDensity: true,
    })

    this.app = app
    container.appendChild(app.canvas)

    this.world = new Container()
    app.stage.addChild(this.world)
    this.fitStage(width, height)
    this.bindPanAndZoom()

    await loadSpineAssets()
    const officeOk = await loadOfficeAssets()
    if (!officeOk) {
      console.error(
        '[Office] desk.png / chair.png 加载失败，工位将使用矢量占位图。请检查 public/assets/office/ 并硬刷新。',
      )
    }

    this.drawMap(this.world)
    this.spawnOffice(this.world)
    this.pushDataToEntities()

    app.ticker.add(this.onTick)
    bindOfficeScene(this)
  }

  /** 导出当前画布为 PNG dataURL（供快捷工具截图下载） */
  captureScreenshot(): string | null {
    if (!this.app) return null
    try {
      const canvas = this.app.renderer.extract.canvas(
        this.app.stage,
      ) as HTMLCanvasElement
      return canvas.toDataURL('image/png')
    } catch (error) {
      console.warn('[OfficeScene] capture screenshot failed', error)
      return null
    }
  }

  /** 名册序号从 1 开始：visitor 去找 host 说一句话后回座继续工作 */
  requestDeskVisit(
    visitorRosterNo: number,
    hostRosterNo: number,
    message: string,
  ) {
    this.agents = this.simulator.startDeskVisit(
      this.agents,
      visitorRosterNo,
      hostRosterNo,
      message,
    )
    this.pushDataToEntities()
  }

  /** 按顺序拜访多个工位，全部说完后回访客工位 */
  requestDeskVisitTour(
    visitorRosterNo: number,
    hostRosterNos: number[],
    messageFn?: (hostRosterNo: number, hostName: string) => string,
  ) {
    this.agents = this.simulator.startDeskVisitTour(
      this.agents,
      visitorRosterNo,
      hostRosterNos,
      messageFn ?? ((hostNo, hostName) => pickHandoffVisitMessage(hostName, hostNo)),
    )
    this.pushDataToEntities()
  }

  getAgents(): Agent[] {
    return this.agents.map((agent) => ({ ...agent }))
  }

  setAgentState(id: string, state: AgentState, task?: string) {
    const current = this.agents.find((a) => a.id === id)
    if (!current) return
    if (this.paused) this.pausedAgentStates.set(id, { state, currentTask: task })

    // 离座小人：先走回座位，到位后再落入目标状态（避免被钉座位瞬移）
    if (!isHomeDeskSeat(current.assignedDeskId, current.x, current.y)) {
      this.agents = startReturnToDesk(this.agents, id, {
        landingState: state,
        task,
      })
      this.agentEntities.get(id)?.hideBubble()
      setOfficeAgents(this.agents)
      this.pushDataToEntities()
      return
    }

    this.agents = this.agents.map((agent) => {
      if (agent.id !== id) return agent
      return {
        ...agent,
        state,
        currentTask: task,
        targetX: undefined,
        targetY: undefined,
        walkPath: undefined,
        walkPathIndex: undefined,
        mission: undefined,
        bubbleText: undefined,
        customAnimation: undefined,
        viewFacing:
          state === 'working' || state === 'thinking'
            ? ('back' as const)
            : agent.viewFacing,
      }
    })
    setOfficeAgents(this.agents)
    this.pushDataToEntities()
  }

  /** 自定义成员姓名后同步场景名牌 */
  setAgentName(id: string, name: string) {
    if (!name || !this.agents.some((a) => a.id === id)) return
    this.agents = this.agents.map((a) => (a.id === id ? { ...a, name } : a))
    setOfficeAgents(this.agents)
    this.pushDataToEntities()
  }

  /** 自定义成员头像 emoji：名牌上叠在名字前，空串恢复默认 */
  setAgentAvatar(id: string, avatar: string) {
    const entity = this.agentEntities.get(id)
    if (!entity) return
    entity.setAvatar(avatar)
  }

  /** 自定义成员人物形象皮肤：空串恢复名册默认分配 */
  setAgentSkin(id: string, skin: string) {
    const entity = this.agentEntities.get(id)
    if (!entity) return
    entity.setSkin(skin)
  }

  /** 全局暂停/继续：暂停时暂存角色状态，继续时恢复暂停前的状态 */
  setPaused(paused: boolean) {
    if (this.paused === paused) return
    this.paused = paused
    if (paused) {
      this.pausedAgentStates = new Map(
        this.agents.map((agent): [string, { state: AgentState; currentTask?: string }] => [
          agent.id,
          { state: agent.state, currentTask: agent.currentTask },
        ]),
      )
      this.agents = this.agents.map((agent) =>
        agent.mission
          ? agent
          : {
              ...agent,
              state: 'idle' as const,
              currentTask: '已暂停',
              bubbleText: undefined,
              customAnimation: undefined,
            },
      )
    } else {
      this.agents = this.agents.map((agent) => {
        if (agent.mission) return agent
        const previous = this.pausedAgentStates.get(agent.id)
        return previous
          ? { ...agent, state: previous.state, currentTask: previous.currentTask }
          : { ...agent, state: 'idle' as const, currentTask: undefined }
      })
      this.pausedAgentStates.clear()
    }
    setOfficeAgents(this.agents)
    this.pushDataToEntities()
  }

  playAgentAnimation(id: string, animation: string, task?: string) {
    const current = this.agents.find((a) => a.id === id)
    if (!current) return

    // 离座小人：先走回座位，到位后再播放表情（避免被钉座位瞬移）
    if (!isHomeDeskSeat(current.assignedDeskId, current.x, current.y)) {
      this.agents = startReturnToDesk(this.agents, id, {
        landingAnimation: animation,
        task,
      })
      this.agentEntities.get(id)?.hideBubble()
      setOfficeAgents(this.agents)
      this.pushDataToEntities()
      return
    }

    this.agents = this.agents.map((agent) => {
      if (agent.id !== id) return agent
      return {
        ...agent,
        state: 'talking' as const,
        currentTask: task,
        targetX: undefined,
        targetY: undefined,
        walkPath: undefined,
        walkPathIndex: undefined,
        mission: undefined,
        bubbleText: undefined,
        customAnimation: animation,
        viewFacing: 'front' as const,
        facing: 1 as const,
      }
    })
    setOfficeAgents(this.agents)
    this.pushDataToEntities()
    this.agentEntities.get(id)?.playCustomAnimation(animation, task)
    this.pullDataFromEntities()
    setOfficeAgents(this.agents)
  }

  resize(containerWidth: number, containerHeight: number) {
    if (!this.app || !this.world) return
    this.app.renderer.resize(containerWidth, containerHeight)
    this.fitStage(containerWidth, containerHeight)
  }

  /** 等比缩放完整办公室场景并居中，任何屏幕比例下都不裁切内容；用户缩放倍数在此基础上叠加 */
  private fitStage(containerWidth: number, containerHeight: number) {
    if (!this.world) return

    this.containerW = containerWidth
    this.containerH = containerHeight
    this.baseScale = Math.min(
      containerWidth / SCENE_WIDTH,
      containerHeight / SCENE_HEIGHT,
    )

    this.world.scale.set(this.baseScale * this.userZoom)
    this.clampPan()

    const canvas = this.app?.canvas as HTMLCanvasElement | undefined
    if (!canvas) return
    canvas.style.display = 'block'
    canvas.style.width = '100%'
    canvas.style.height = '100%'
    canvas.style.maxWidth = '100%'
    canvas.style.maxHeight = '100%'
  }

  /** 夹紧平移：场景缩到能放下时回中；放大时四边不允许拖出空洞 */
  private clampPan() {
    if (!this.world) return
    const scale = this.baseScale * this.userZoom
    const worldW = SCENE_WIDTH * scale
    const worldH = SCENE_HEIGHT * scale
    const x =
      worldW <= this.containerW
        ? (this.containerW - worldW) / 2
        : Math.min(0, Math.max(this.containerW - worldW, this.world.x))
    const y =
      worldH <= this.containerH
        ? (this.containerH - worldH) / 2
        : Math.min(0, Math.max(this.containerH - worldH, this.world.y))
    this.world.position.set(x, y)
  }

  /** 设置用户缩放（1~2）。传入 pivot 时保持该屏幕点下的场景内容不动（滚轮缩放用） */
  setUserZoom(zoom: number, pivotX?: number, pivotY?: number) {
    if (!this.world || !this.app) return
    const next = Math.min(2, Math.max(1, zoom))
    const oldScale = this.baseScale * this.userZoom
    const newScale = this.baseScale * next
    if (Math.abs(newScale - oldScale) < 1e-6) return

    if (pivotX !== undefined && pivotY !== undefined && next > 1) {
      const sceneX = (pivotX - this.world.x) / oldScale
      const sceneY = (pivotY - this.world.y) / oldScale
      this.world.scale.set(newScale)
      this.world.position.set(
        pivotX - sceneX * newScale,
        pivotY - sceneY * newScale,
      )
    } else {
      this.world.scale.set(newScale)
    }
    this.userZoom = next
    this.clampPan()
    this.updateCursor()
    this.zoomListener?.(next)
  }

  getUserZoom() {
    return this.userZoom
  }

  /** 滚轮缩放时同步外部滑杆显示 */
  setZoomListener(listener: ((zoom: number) => void) | null) {
    this.zoomListener = listener
  }

  private updateCursor() {
    const canvas = this.app?.canvas as HTMLCanvasElement | undefined
    if (!canvas) return
    canvas.style.cursor = this.panDrag
      ? 'grabbing'
      : this.userZoom > 1
        ? 'grab'
        : ''
  }

  /** 滚轮缩放（以指针位置为中心）+ 放大后按住拖动平移（任意方向） */
  private bindPanAndZoom() {
    if (!this.app) return
    const canvas = this.app.canvas as HTMLCanvasElement
    // stage 全屏可命中，才能监听空白地面的拖动；小人等子元素命中优先级不变
    this.app.stage.eventMode = 'static'
    this.app.stage.hitArea = this.app.screen
    canvas.addEventListener('wheel', this.onWheel, { passive: false })
    this.app.stage.on('pointerdown', this.onPanStart)
    window.addEventListener('pointermove', this.onPanMove)
    window.addEventListener('pointerup', this.onPanEnd)
    window.addEventListener('pointercancel', this.onPanEnd)
  }

  private onWheel = (event: WheelEvent) => {
    if (!this.app) return
    event.preventDefault()
    const rect = (this.app.canvas as HTMLCanvasElement).getBoundingClientRect()
    const factor = Math.exp(-event.deltaY * 0.0012)
    this.setUserZoom(
      this.userZoom * factor,
      event.clientX - rect.left,
      event.clientY - rect.top,
    )
  }

  private onPanStart = (event: FederatedPointerEvent) => {
    if (!this.world || this.userZoom <= 1 || event.button !== 0) return
    this.panDrag = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      worldX: this.world.x,
      worldY: this.world.y,
      moved: false,
    }
    this.updateCursor()
  }

  private onPanMove = (event: PointerEvent) => {
    const drag = this.panDrag
    if (!drag || event.pointerId !== drag.pointerId || !this.world) return
    const dx = event.clientX - drag.startX
    const dy = event.clientY - drag.startY
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return
    drag.moved = true
    this.world.position.set(drag.worldX + dx, drag.worldY + dy)
    this.clampPan()
  }

  private onPanEnd = (event: PointerEvent) => {
    const drag = this.panDrag
    if (!drag || event.pointerId !== drag.pointerId) return
    if (drag.moved) this.lastPanEndAt = performance.now()
    this.panDrag = null
    this.updateCursor()
  }

  destroy() {
    bindOfficeScene(null)
    const canvas = this.app?.canvas as HTMLCanvasElement | undefined
    canvas?.removeEventListener('wheel', this.onWheel)
    this.app?.stage.off('pointerdown', this.onPanStart)
    window.removeEventListener('pointermove', this.onPanMove)
    window.removeEventListener('pointerup', this.onPanEnd)
    window.removeEventListener('pointercancel', this.onPanEnd)
    this.zoomListener = null
    this.app?.ticker.remove(this.onTick)
    this.app?.destroy(true, { children: true })
    this.app = null
    this.agentEntities.clear()
    this.deskEntities.clear()
    this.officeLayer = null
    this.nightLayer = null
    this.nightLights = []
    this.floorGraphics = null
  }

  private onTick = (ticker: { deltaTime: number }) => {
    const dt = Math.min(ticker.deltaTime / 60, 0.05)

    this.agents = this.simulator.tick(dt, this.agents)
    this.pushDataToEntities()

    this.movement.update(this.agentEntities, dt)
    this.pullDataFromEntities()

    this.agents = this.simulator.afterMovement(
      dt,
      this.agents,
      this.agentEntities,
    )
    this.pushDataToEntities()

    // 先同步 store 并消费外部队列，再补 Demo，避免空闲窗口被自动工作流吞掉
    setOfficeAgents(this.agents)
    notifyVisitMissionActivity(this.agents)
    if (!this.paused) this.updateAutoWorkflow(dt)

    this.animation.update(this.agentEntities, dt)
    this.sortOfficeDepth()
    this.syncDeskOccupancy()

    // 夜晚灯光呼吸：轻微明暗起伏，让房间有「活」的灯光感
    if (this.nightMode && this.nightLayer?.visible) {
      this.nightTime += dt
      for (const light of this.nightLights) {
        light.sprite.alpha =
          light.baseAlpha *
          (1 + Math.sin(this.nightTime * light.speed + light.phase) * 0.05)
      }
    }

    // 暂停锁：无行程的小人强制空闲，防止 WS 事件/模拟循环把他们刷回工作状态
    if (this.paused) {
      this.agents = this.agents.map((agent) =>
        agent.mission
          ? agent
          : {
              ...agent,
              state: 'idle' as const,
              currentTask: '已暂停',
              bubbleText: undefined,
              customAnimation: undefined,
            },
      )
      this.pushDataToEntities()
    }

    setOfficeAgents(this.agents)
  }

  private updateAutoWorkflow(dt: number) {
    // 接入真实 LangGraph 后端后，假演示工作流默认关闭
    if (!OFFICE_AUTO_DEMO) return

    this.autoWorkflowTimer -= dt
    if (this.autoWorkflowTimer > 0) return

    // 外部 HTTP 拜访优先：有待执行命令时不启动新的 Demo 拜访
    if (hasPendingVisitQueue()) {
      this.autoWorkflowTimer = 0.35
      return
    }

    const activeCount = this.agents.filter((agent) => agent.mission).length
    if (activeCount >= AUTO_WORKFLOW_MAX_ACTIVE) {
      this.autoWorkflowTimer = 0.35
      return
    }

    const step = this.nextAvailableWorkflowStep()
    if (!step) {
      this.autoWorkflowTimer = 0.45
      return
    }

    const visitorName = AGENT_ROSTER[step.visitor - 1]?.name ?? `#${step.visitor}`
    const message = `${visitorName}：${step.message}`
    this.agents = this.simulator.startDeskVisit(
      this.agents,
      step.visitor,
      step.host,
      message,
    )
    this.pushDataToEntities()
    this.autoWorkflowTimer = AUTO_WORKFLOW_INTERVAL
  }

  private nextAvailableWorkflowStep(): AutoWorkflowStep | null {
    for (let i = 0; i < AUTO_WORKFLOW_STEPS.length; i++) {
      const index = (this.autoWorkflowIndex + i) % AUTO_WORKFLOW_STEPS.length
      const step = AUTO_WORKFLOW_STEPS[index]!
      if (this.canStartWorkflowStep(step)) {
        this.autoWorkflowIndex = (index + 1) % AUTO_WORKFLOW_STEPS.length
        return step
      }
    }
    return null
  }

  private canStartWorkflowStep(step: AutoWorkflowStep): boolean {
    const visitor = this.agents[step.visitor - 1]
    const host = this.agents[step.host - 1]
    if (!visitor || !host || visitor.id === host.id) return false

    const busyIds = new Set<string>()
    for (const agent of this.agents) {
      if (agent.mission) {
        busyIds.add(agent.id)
        busyIds.add(agent.mission.hostAgentId)
      }
      if (agent.state === 'walking' || agent.customAnimation) {
        busyIds.add(agent.id)
      }
    }

    return !busyIds.has(visitor.id) && !busyIds.has(host.id)
  }

  private sortOfficeDepth() {
    if (!this.officeLayer) return

    const agentPositions = [...this.agentEntities.values()].map((e) => ({
      x: e.position.x,
      y: e.position.y,
    }))

    for (const e of this.agentEntities.values()) {
      e.zIndex = e.position.y
    }

    for (const desk of this.deskEntities.values()) {
      desk.updateDepthZ(agentPositions)
    }

    this.officeLayer.sortChildren()
  }

  private pushDataToEntities() {
    for (const agent of this.agents) {
      const entity = this.agentEntities.get(agent.id)
      if (!entity) continue

      const prev = entity.data
      entity.apply(agent)
      if (
        prev.x !== agent.x ||
        prev.y !== agent.y ||
        agent.state !== 'walking'
      ) {
        entity.setPosition(agent.x, agent.y)
      }
    }
  }

  private pullDataFromEntities() {
    this.agents = this.agents.map((agent) => {
      const entity = this.agentEntities.get(agent.id)
      return entity ? { ...agent, ...entity.data } : agent
    })
  }

  private syncDeskOccupancy() {
    const occupied = new Set(
      this.agents
        .filter((a) => a.state === 'working' && a.assignedDeskId)
        .map((a) => a.assignedDeskId!),
    )
    for (const desk of this.deskEntities.values()) {
      desk.setOccupied(occupied.has(desk.deskId))
    }
  }

  /** 桌子 / 人物 / 椅子同层；桌沿为界动态遮挡 */
  private spawnOffice(parent: Container) {
    const layer = new Container()
    layer.label = 'office'
    layer.sortableChildren = true
    this.officeLayer = layer

    // 姓名/任务标签单独一层，压在桌椅之上：坐下办公时显示器会盖住人物本体，标签不能跟着被挡住
    const labelLayer = new Container()
    labelLayer.label = 'labels'
    labelLayer.sortableChildren = true

    for (const desk of DESKS) {
      const entity = new DeskEntity(desk)
      this.deskEntities.set(desk.id, entity)
      layer.addChild(
        entity.shadowGfx,
        entity.deskLayer,
        entity.chairLayer,
        entity.occupiedIndicator,
      )
    }

    for (const agent of this.agents) {
      const entity = new AgentEntity(agent, labelLayer)
      this.agentEntities.set(agent.id, entity)
      entity.zIndex = agent.y
      entity.on('pointertap', (event: FederatedPointerEvent) => {
        // 拖动平移刚结束时不触发小人菜单，避免把拖拽误判成点击
        if (performance.now() - this.lastPanEndAt < 250) return
        event.stopPropagation()
        this.options.onAgentClick?.({
          agent: { ...entity.data },
          rosterNo: this.agents.findIndex((a) => a.id === agent.id) + 1,
          clientX: event.clientX,
          clientY: event.clientY,
        })
      })
      layer.addChild(entity)
    }

    this.sortOfficeDepth()
    parent.addChild(layer, labelLayer)
  }

  private drawMap(parent: Container) {
    const map = new Container()
    map.label = 'map'

    const floor = new Graphics()
    floor.rect(0, 0, SCENE_WIDTH, SCENE_HEIGHT)
    // 初始即按当前主题铺色：暗色重建后地板仍是夜晚色
    floor.fill(this.nightMode ? NIGHT_BG_COLOR : COLORS.floor)
    map.addChild(floor)
    this.floorGraphics = floor

    const bgTex = getOfficeBackgroundTexture()
    if (bgTex) {
      const bg = new Sprite(bgTex)
      const scale = Math.min(
        SCENE_WIDTH / bgTex.width,
        SCENE_HEIGHT / bgTex.height,
      )
      bg.scale.set(scale)
      bg.position.set(
        (SCENE_WIDTH - bgTex.width * scale) / 2,
        (SCENE_HEIGHT - bgTex.height * scale) / 2,
      )
      map.addChild(bg)
    }

    parent.addChildAt(map, 0)
  }
}
