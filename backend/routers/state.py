"""状态查询、系统资源、搜索、文件中心与健康检查路由。

- GET /api/state      当前 Sprint 快照
- GET /api/sprints    历史 Sprint 列表
- GET /api/sprints/{id} 单个 Sprint 详情
- DELETE /api/sprints/{id} 删除历史 Sprint
- GET /api/events     事件时间线
- GET /api/system     真实系统资源（CPU/内存/网络）
- GET /api/search     全局搜索
- GET /api/locations  系统目录列表
- GET /api/files      当前 Sprint 产物与日报
- GET /api/health     健康检查
"""
from __future__ import annotations

import time

import psutil
from fastapi import APIRouter, HTTPException

import forge
import llm
import storage
import artifacts as artifact_store
from artifacts import artifacts_store
import events as bus
from events import sprint_meta, tasks_store
from llm import llm_available
from route_helpers import _active_round, _sprint_view, build_report_md
import runtime_state

router = APIRouter(tags=["state"])


@router.get("/api/state")
async def get_state():
    sprint_id = sprint_meta.get("id") if runtime_state.running else None
    baseline = sprint_meta.get("usage_baseline") or 0.0
    active = _active_round(runtime_state.running)
    return {
        "running": runtime_state.running,
        "paused": bus.is_paused(),
        "llm": llm_available(),
        "tasks": active["tasks"],
        "artifacts": active["artifacts"],
        "sprint": active["sprint"],
        "requirement_contract": (_sprint_view(sprint_id) or {}).get("contract", {}),
        "handoffs": storage.list_handoffs(sprint_id) if sprint_id else [],
        "cost": max(0.0, round(llm.usage_total_cost() - baseline, 4)) if runtime_state.running else 0,
        "history": storage.list_sprints(20),
    }


@router.get("/api/sprints")
async def list_sprints():
    return {"sprints": storage.list_sprints(100)}


@router.get("/api/sprints/{sprint_id}")
async def get_sprint_detail(sprint_id: str):
    sprint = _sprint_view(sprint_id)
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint 不存在")
    return {
        "sprint": sprint,
        "tasks": storage.list_tasks(sprint_id),
        "handoffs": storage.list_handoffs(sprint_id),
        "artifacts": storage.list_artifacts(sprint_id),
        "events": storage.list_events(sprint_id, 200),
    }


@router.delete("/api/sprints/{sprint_id}")
async def delete_sprint(sprint_id: str):
    """删除一条历史 Sprint 及其任务/产物/事件，并清理磁盘关联文件与代码目录。"""
    if sprint_id == sprint_meta.get("id") and runtime_state.running:
        raise HTTPException(status_code=409, detail="该 Sprint 正在进行中，不能删除")
    if not storage.get_sprint(sprint_id):
        raise HTTPException(status_code=404, detail="Sprint 不存在")
    files = storage.delete_sprint(sprint_id)
    removed = 0
    for name in files:
        path = forge.safe_deliverable_path(name)
        if path:
            try:
                import os
                os.unlink(path)
                removed += 1
            except OSError:
                pass
    code_dir = forge.sprint_code_dir(sprint_id)
    import os
    if os.path.isdir(code_dir):
        import shutil
        shutil.rmtree(code_dir, ignore_errors=True)
    if sprint_id == sprint_meta.get("id"):
        tasks_store.clear()
        artifact_store.clear_current_sprint()
        bus.event_log.clear()
        sprint_meta.update({"id": "", "requirement": "", "status": "idle", "stage": ""})
    return {"ok": True, "removed_files": removed}


@router.get("/api/events")
async def get_events():
    return {"events": bus.event_log}


@router.get("/api/system")
async def system_stats():
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory().percent
    io = psutil.net_io_counters()
    now = time.time()
    total = io.bytes_sent + io.bytes_recv
    elapsed = max(now - runtime_state.net_last["ts"], 1e-6)
    rate_kb_s = max(total - runtime_state.net_last["bytes"], 0) / elapsed / 1024
    runtime_state.net_last = {"ts": now, "bytes": total}
    return {
        "cpu": round(cpu, 1),
        "mem": round(mem, 1),
        "net_kb_s": round(rate_kb_s, 1),
    }


@router.get("/api/search")
async def global_search(q: str = ""):
    """全局搜索：覆盖历史需求、任务、产物与真实文件名。"""
    keyword = q.strip()
    if not keyword:
        return {"sprints": [], "tasks": [], "artifacts": []}
    return storage.search(keyword)


@router.get("/api/locations")
async def list_locations():
    """真实系统目录（桌面/文档/下载等），供「保存位置」下拉选择。"""
    return {"locations": forge.list_known_locations()}


@router.get("/api/files")
async def files():
    if not runtime_state.running:
        return {
            "requirement": "",
            "running": False,
            "updated_at": 0,
            "tasks": [],
            "artifacts": [],
            "report_md": "",
        }
    return {
        "requirement": sprint_meta["requirement"],
        "running": runtime_state.running,
        "updated_at": sprint_meta["finished_at"] or sprint_meta["started_at"],
        "tasks": tasks_store,
        "artifacts": artifacts_store,
        "report_md": build_report_md(sprint_meta["requirement"], tasks_store, runtime_state.running),
    }


@router.get("/api/health")
async def health():
    from events import clients
    return {
        "ok": True,
        "running": runtime_state.running,
        "llm": llm_available(),
        "ws_clients": len(clients),
        "ts": time.time(),
    }
