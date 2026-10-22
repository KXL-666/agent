"""事件总线：LangGraph 产生的事件实时推送给所有 WebSocket 客户端

同时把任务更新写入 SQLite，保证刷新/重启后可恢复当前 Sprint 的看板。
"""

import asyncio
import json
import time

from fastapi import WebSocket

import storage

clients: set[WebSocket] = set()

# 当前 Sprint 的任务快照（供 Dashboard / 新连接初始化用）
tasks_store: list[dict] = []

# 当前 Sprint 元信息（供文件中心 / 进度条 / 日报导出用）
sprint_meta: dict = {
    "id": "",
    "requirement": "",
    "started_at": 0.0,
    "finished_at": 0.0,
    "status": "idle",
    "stage": "",
    "stage_started": 0.0,
    "deliver_kind": "",
    "usage_baseline": 0.0,
    "fast": False,
}

# 最近事件环形日志（调试与后续接任务面板）
event_log: list[dict] = []
EVENT_LOG_LIMIT = 300

# 全局暂停闸门：暂停期间所有节点在下一个检查点阻塞，不再产生 LLM 调用（真正省钱）
paused = False


def set_paused(value: bool) -> None:
    global paused
    paused = bool(value)


def is_paused() -> bool:
    return paused


async def wait_if_paused() -> None:
    """工作流在每次 LLM 调用前调用：暂停时挂起等待，继续后自动放行"""
    while paused:
        await asyncio.sleep(0.4)


def load_persisted(sprint_id: str) -> None:
    """重启/切换 Sprint 后，从数据库恢复事件日志（通知中心与时间线不再清空）"""
    if not sprint_id:
        return
    try:
        stored = storage.list_events(sprint_id, EVENT_LOG_LIMIT)
    except Exception as error:  # noqa: BLE001
        print(f"[Events] 恢复历史事件失败：{error}")
        return
    event_log[:] = stored


async def broadcast(event: dict) -> None:
    if not clients:
        return
    data = json.dumps(event, ensure_ascii=False)
    dead = []
    for ws in list(clients):
        try:
            await ws.send_text(data)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


def emit(event: dict) -> None:
    """在事件循环内调用：记录并广播一条事件"""
    event.setdefault("ts", time.time())
    event_log.append(event)
    if len(event_log) > EVENT_LOG_LIMIT:
        del event_log[: len(event_log) - EVENT_LOG_LIMIT]
    # 任务更新同步落库（sprint_meta.id 存在时）
    if event.get("type") == "task_update" and sprint_meta["id"]:
        try:
            storage.upsert_task(sprint_meta["id"], event.get("task") or {})
        except Exception as error:  # noqa: BLE001 —— 持久化失败不阻断实时推送
            print(f"[Events] 任务落库失败：{error}")
    # 关键事件落库：刷新/重连后可恢复通知与时间线
    try:
        storage.add_event(sprint_meta.get("id", ""), event)
    except Exception as error:  # noqa: BLE001
        print(f"[Events] 事件落库失败：{error}")
    asyncio.ensure_future(broadcast(event))


def set_stage(stage: str) -> None:
    """更新并广播当前 Sprint 所处阶段（用于顶部进度条）"""
    sprint_meta["stage"] = stage
    sprint_meta["stage_started"] = time.time()
    if sprint_meta["id"]:
        storage.update_sprint(sprint_meta["id"], stage=stage)
    emit(
        {
            "type": "stage",
            "stage": stage,
            "started_at": sprint_meta["started_at"],
            "stage_started": sprint_meta["stage_started"],
        }
    )
