import { useEffect, useRef } from 'react'
import { OFFICE_WS_URL } from '@/config/officeMode'
import { OfficeWsClient } from '@/services/officeWsClient'

/** 连接 LangGraph 后端 WebSocket，将真实协作事件同步到办公室场景 */
export function OfficeActionConnector() {
  const clientRef = useRef<OfficeWsClient | null>(null)

  useEffect(() => {
    const client = new OfficeWsClient()
    clientRef.current = client
    client.connect(OFFICE_WS_URL)

    return () => {
      client.disconnect()
      clientRef.current = null
    }
  }, [])

  return null
}
