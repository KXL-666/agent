import { useEffect, useRef, useState } from 'react'
import { OfficeScene, type OfficeAgentClick } from '@/scene/OfficeScene'
import type { Agent, AgentState } from '@/types/agent'

type AgentMenuState = {
  agent: Agent
  rosterNo: number
  x: number
  y: number
  agents: Agent[]
  pickingTarget: boolean
}

const STATE_ACTIONS: Array<{
  label: string
  state: AgentState
  task?: string
}> = [
  { label: '开始工作', state: 'working', task: '处理当前任务…' },
  { label: '进入思考', state: 'thinking', task: '思考下一步…' },
  { label: '暂时空闲', state: 'idle' },
]

const EMOTE_ACTIONS = [
  { label: '生气', animation: 'emotes/angry' },
  { label: '打嗝', animation: 'emotes/burp' },
  { label: '困惑', animation: 'emotes/confused' },
  { label: '哭泣', animation: 'emotes/crying' },
  { label: '倒下', animation: 'emotes/dead' },
  { label: '坚定', animation: 'emotes/determined' },
  { label: '凝视', animation: 'emotes/dramatic-stare' },
  { label: '兴奋', animation: 'emotes/excited' },
  { label: '撒娇', animation: 'emotes/fawning' },
  { label: '脸红', animation: 'emotes/flushed' },
  { label: '欢呼', animation: 'emotes/hooray' },
  { label: '灵感', animation: 'emotes/idea' },
  { label: '刚刚好', animation: 'emotes/just-right' },
  { label: '大笑', animation: 'emotes/laugh' },
  { label: '喜欢', animation: 'emotes/love' },
  { label: '害怕', animation: 'emotes/scared' },
  { label: '遮眼', animation: 'emotes/see-no-evil' },
  { label: '耸肩', animation: 'emotes/shrug' },
  { label: '闷闷不乐', animation: 'emotes/sulk' },
  { label: '冒汗', animation: 'emotes/sweat' },
  { label: '思考表情', animation: 'emotes/thinking' },
  { label: '吐舌', animation: 'emotes/tongue-out' },
  { label: '挥手', animation: 'emotes/wave' },
] as const

export function OfficeCanvas() {
  const hostRef = useRef<HTMLDivElement>(null)
  const sceneRef = useRef<OfficeScene | null>(null)
  const readyRef = useRef(false)
  const [menu, setMenu] = useState<AgentMenuState | null>(null)
  const [zoom, setZoom] = useState(1)
  const [sceneReady, setSceneReady] = useState(false)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return

    const handleAgentClick = (event: OfficeAgentClick) => {
      const rect = host.getBoundingClientRect()
      const menuWidth = 260
      const menuHeight = Math.min(520, rect.height - 24)
      setMenu({
        agent: event.agent,
        rosterNo: event.rosterNo,
        x: Math.max(12, Math.min(event.clientX - rect.left, rect.width - menuWidth - 12)),
        y: Math.max(12, Math.min(event.clientY - rect.top, rect.height - menuHeight - 12)),
        agents: sceneRef.current?.getAgents() ?? [],
        pickingTarget: false,
      })
    }

    const scene = new OfficeScene({ onAgentClick: handleAgentClick })
    sceneRef.current = scene
    // 滚轮缩放时同步滑杆位置
    scene.setZoomListener(setZoom)
    // 开发环境暴露场景句柄，便于控制台调试与自动化验证
    if (import.meta.env.DEV) {
      ;(window as unknown as Record<string, unknown>).__officeScene = scene
    }

    const ro = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect
      if (width <= 0 || height <= 0) return

      if (!readyRef.current) {
        readyRef.current = true
        void scene.init(host, width, height).then(() => setSceneReady(true))
        return
      }

      sceneRef.current?.resize(width, height)
    })

    ro.observe(host)

    return () => {
      ro.disconnect()
      readyRef.current = false
      setSceneReady(false)
      scene.destroy()
      sceneRef.current = null
    }
  }, [])

  useEffect(() => {
    const close = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Element && target.closest('.agent-action-menu')) {
        return
      }
      setMenu(null)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenu(null)
    }

    document.addEventListener('pointerdown', close)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', close)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [])

  const applyState = (state: AgentState, task?: string) => {
    if (!menu) return
    sceneRef.current?.setAgentState(menu.agent.id, state, task)
    setMenu(null)
  }

  const playEmote = (animation: string, label: string) => {
    if (!menu) return
    sceneRef.current?.playAgentAnimation(menu.agent.id, animation, label)
    setMenu(null)
  }

  const startInteraction = (targetRosterNo: number, targetName: string) => {
    if (!menu || targetRosterNo === menu.rosterNo) return
    sceneRef.current?.requestDeskVisit(
      menu.rosterNo,
      targetRosterNo,
      `${targetName}，我来和你同步一下。`,
    )
    setMenu(null)
  }

  return (
    <div ref={hostRef} className="office-canvas">
      {sceneReady ? (
        <div className="office-zoom-controls">
          <button
            type="button"
            aria-label="缩小"
            disabled={zoom <= 1.001}
            onClick={() =>
              sceneRef.current?.setUserZoom(Math.max(1, zoom - 0.2))
            }
          >
            −
          </button>
          <input
            type="range"
            min={1}
            max={2}
            step={0.05}
            value={zoom}
            aria-label="场景缩放"
            onChange={(event) =>
              sceneRef.current?.setUserZoom(Number(event.target.value))
            }
          />
          <button
            type="button"
            aria-label="放大"
            disabled={zoom >= 1.999}
            onClick={() =>
              sceneRef.current?.setUserZoom(Math.min(2, zoom + 0.2))
            }
          >
            ＋
          </button>
          <span className="office-zoom-value">{Math.round(zoom * 100)}%</span>
          {zoom > 1.001 ? (
            <button
              type="button"
              className="office-zoom-reset"
              onClick={() => sceneRef.current?.setUserZoom(1)}
            >
              复位
            </button>
          ) : null}
        </div>
      ) : null}
      {menu && (
        <div
          className="agent-action-menu"
          style={{ left: menu.x, top: menu.y }}
        >
          <div className="agent-action-head">
            <span className="agent-action-name">{menu.agent.name}</span>
            <span className="agent-action-state">{menu.agent.state}</span>
          </div>

          {!menu.pickingTarget ? (
            <>
              <div className="agent-action-group">
                <div className="agent-action-section-title">互动</div>
                <button
                  type="button"
                  className="agent-action-btn"
                  onClick={() =>
                    setMenu((current) =>
                      current ? { ...current, pickingTarget: true } : current,
                    )
                  }
                >
                  互动…
                </button>
              </div>

              <div className="agent-action-group">
                <div className="agent-action-section-title">状态</div>
                {STATE_ACTIONS.map((action) => (
                  <button
                    key={action.label}
                    type="button"
                    className="agent-action-btn"
                    onClick={() => applyState(action.state, action.task)}
                  >
                    {action.label}
                  </button>
                ))}
              </div>

              <div className="agent-action-group">
                <div className="agent-action-section-title">表情动作</div>
                <div className="agent-emote-grid">
                  {EMOTE_ACTIONS.map((action) => (
                    <button
                      key={action.animation}
                      type="button"
                      className="agent-action-chip"
                      onClick={() => playEmote(action.animation, action.label)}
                    >
                      {action.label}
                    </button>
                  ))}
                </div>
              </div>
            </>
          ) : (
            <div className="agent-action-group">
              <div className="agent-action-section-title">选择互动对象</div>
              <button
                type="button"
                className="agent-action-btn subtle"
                onClick={() =>
                  setMenu((current) =>
                    current ? { ...current, pickingTarget: false } : current,
                  )
                }
              >
                返回动作
              </button>
              {menu.agents
                .map((agent, index) => ({ agent, rosterNo: index + 1 }))
                .filter(({ agent }) => agent.id !== menu.agent.id)
                .map(({ agent, rosterNo }) => (
                  <button
                    key={agent.id}
                    type="button"
                    className="agent-action-btn"
                    onClick={() => startInteraction(rosterNo, agent.name)}
                  >
                    和 {agent.name} 互动
                  </button>
                ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
