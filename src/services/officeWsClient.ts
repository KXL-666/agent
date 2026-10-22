import { OFFICE_WS_URL } from '@/config/officeMode'
import { dispatchOfficeAction } from '@/services/officeActionDispatch'

const RECONNECT_BASE_MS = 2000
const RECONNECT_MAX_MS = 15000

/** LangGraph 后端 WebSocket 客户端：断线自动重连，收到事件即分发到场景 */
export class OfficeWsClient {
  private ws: WebSocket | null = null
  private url: string = OFFICE_WS_URL
  private shouldConnect = false
  private reconnectAttempt = 0

  connect(url: string = OFFICE_WS_URL) {
    this.url = url
    this.shouldConnect = true
    this.open()
  }

  disconnect() {
    this.shouldConnect = false
    this.ws?.close()
    this.ws = null
  }

  private open() {
    if (!this.shouldConnect) return

    try {
      this.ws = new WebSocket(this.url)
    } catch (error) {
      console.warn('[OfficeWS] create failed, will retry', this.url, error)
      this.scheduleReconnect()
      return
    }

    const ws = this.ws
    ws.onopen = () => {
      this.reconnectAttempt = 0
      console.info('[OfficeWS] connected', this.url)
    }
    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(String(event.data))
        dispatchOfficeAction(message)
      } catch {
        console.warn('[OfficeWS] invalid message', event.data)
      }
    }
    ws.onerror = () => {
      ws.close()
    }
    ws.onclose = () => {
      if (this.shouldConnect) {
        console.info('[OfficeWS] closed, retrying…')
        this.scheduleReconnect()
      }
    }
  }

  private scheduleReconnect() {
    const delay = Math.min(
      RECONNECT_BASE_MS * 2 ** this.reconnectAttempt,
      RECONNECT_MAX_MS,
    )
    this.reconnectAttempt += 1
    setTimeout(() => this.open(), delay)
  }
}
