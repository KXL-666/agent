"""多 Agent 敏捷开发系统 — 后端入口

启动：python -m uvicorn main:app --host 127.0.0.1 --port 8000

路由已按业务域拆分到 routers/ 包，本文件仅负责：
- 创建 FastAPI 实例与 CORS 中间件
- 启动时恢复上一次 Sprint 的看板与产物
- 注册各业务路由（include_router）
- 对外 re-export 测试依赖的符号（app、_detect_file_kind 等）

各路由实现见：
- routers/sprint.py     Sprint 生命周期与任务管理
- routers/uploads.py    附件上传
- routers/state.py      状态/系统/搜索/文件/健康
- routers/artifacts.py  产物中心
- routers/chat.py       角色对话
- routers/solo.py       单兵任务（单飞）
- routers/agents.py     成员配置
- routers/usage.py      用量与单价
- routers/backup.py     数据备份
- routers/ws.py         WebSocket
- skills_api.py         技能管理
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import agent_config
import artifacts as artifact_store
import events as bus
import forge
import llm
import storage
from events import sprint_meta, tasks_store
from graph import build_sprint_graph
from roles import ROSTER

import runtime_state

agent_config.apply_saved_names()  # 恢复用户保存的成员姓名

# 启动时恢复上一次 Sprint 的看板与产物，刷新/重启后不再"失忆"
_last = storage.latest_sprint()
if _last:
    sprint_meta.update(
        {
            "id": _last["id"],
            "requirement": _last["requirement"],
            "started_at": _last["started_at"],
            "finished_at": _last["finished_at"],
            "status": _last["status"],
            "stage": _last["stage"],
            "deliver_kind": _last["deliver_kind"],
        }
    )
    tasks_store.extend(storage.list_tasks(_last["id"]))
    artifact_store.set_sprint(_last["id"])
    forge.begin_sprint(_last["id"])
    bus.load_persisted(_last["id"])  # 恢复历史事件：刷新/重启后通知与时间线不再清空


@asynccontextmanager
async def lifespan(_: FastAPI):
    import archive_layout
    import file_knowledge
    import revision_jobs
    revision_jobs.recover_interrupted()
    await asyncio.to_thread(archive_layout.migrate)
    await asyncio.to_thread(file_knowledge.index_existing)
    if sprint_meta.get("id"):
        artifact_store.set_sprint(sprint_meta["id"])
    yield
    await revision_jobs.shutdown()
    if runtime_state.sprint_task and not runtime_state.sprint_task.done():
        runtime_state.sprint_task.cancel()


app = FastAPI(title="AI 敏捷开发办公室", lifespan=lifespan)

# 技能管理路由（已有先例，保持原样）
from skills_api import router as skills_router
app.include_router(skills_router)

# 按业务域拆分的路由
from routers.sprint import router as sprint_router
from routers.uploads import router as uploads_router
from routers.state import router as state_router
from routers.artifacts import router as artifacts_router
from routers.chat import router as chat_router
from routers.solo import router as solo_router
from routers.agents import router as agents_router
from routers.usage import router as usage_router
from routers.backup import router as backup_router
from routers.ws import router as ws_router
from routers.revision import router as revision_router

app.include_router(sprint_router)
app.include_router(uploads_router)
app.include_router(state_router)
app.include_router(artifacts_router)
app.include_router(chat_router)
app.include_router(solo_router)
app.include_router(agents_router)
app.include_router(usage_router)
app.include_router(backup_router)
app.include_router(ws_router)
app.include_router(revision_router)

# 仅允许本机开发/预览前端跨域访问，避免任意站点调用本机 API
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 向后兼容：测试文件通过 main.<symbol> 引用的符号在此 re-export
# ---------------------------------------------------------------------------
from graph import BudgetExceeded  # noqa: E402
from llm import chat_json, chat_text, stream_chat  # noqa: E402
from route_helpers import (  # noqa: E402
    _detect_file_kind,
    _find_artifact,
    _local_model_ready,
)
from routers.artifacts import (  # noqa: E402
    RevisionRequest,
    artifact_provenance,
    revise_artifact,
)
from sprint_runner import _manual_call_guard  # noqa: E402

__all__ = [
    "app",
    "llm",
    "chat_json",
    "chat_text",
    "stream_chat",
    "BudgetExceeded",
    "RevisionRequest",
    "_detect_file_kind",
    "_find_artifact",
    "_local_model_ready",
    "_manual_call_guard",
    "revise_artifact",
    "artifact_provenance",
    "ROSTER",
]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
