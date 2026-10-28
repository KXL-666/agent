"""办公室实时 WebSocket 路由。

- WS /ws/office  实时接收办公室动作事件（hello 含完整快照）
"""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import llm
import events as bus
from events import clients
from llm import llm_available
from route_helpers import _active_round
import runtime_state

router = APIRouter(tags=["ws"])


@router.websocket("/ws/office")
async def ws_office(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    print(f"[WS] 客户端接入，当前 {len(clients)} 个")
    try:
        active = _active_round(runtime_state.running)
        await ws.send_json(
            {
                "type": "hello",
                "llm": llm_available(),
                "running": runtime_state.running,
                "paused": bus.is_paused(),
                "tasks": active["tasks"],
                "artifacts": active["artifacts"],
                "sprint": active["sprint"],
                "events": bus.event_log[-80:],
                "usage": llm.usage_report(),
            }
        )
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        print(f"[WS] 客户端断开，剩余 {len(clients)} 个")
