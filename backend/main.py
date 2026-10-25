"""多 Agent 敏捷开发系统 — 后端入口

启动：python -m uvicorn main:app --host 127.0.0.1 --port 8000
- WS   ws://127.0.0.1:8000/ws/office   实时接收办公室动作事件（hello 含完整快照）
- POST /api/sprint/start               {"requirement": "需求描述", "fast": false} 发起一次 Sprint
- GET  /api/sprints                    历史 Sprint 列表（SQLite 持久化）
- GET  /api/sprints/{id}               单个 Sprint 的任务与产物
- GET  /api/state                      当前 Sprint 快照（任务/产物/阶段/花费）
- POST /api/artifacts/{id}/retry       重新生成单个产物
- POST /api/artifacts/{id}/revise      基于该产物继续修改，生成新版本
- GET  /api/health                     健康检查
- GET  /api/system                     真实系统资源（CPU/内存/网络）
- POST /api/chat                       {"roster","message","history"} 角色聊天
- GET  /api/files                       当前 Sprint 产物（需求/任务/日报）
"""

import asyncio
import io
import json
import os
import shutil
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

import agent_config
from chat_context import build_chat_system, clock_reply, unavailable_reply
import artifacts as artifact_store
import demo
import events as bus
import forge
import llm
import storage
import skills
import uploads
import file_knowledge
from requirement_contract import build_contract
from urllib.parse import urlsplit
from artifacts import add_artifact, artifacts_store
from events import clients, sprint_meta, tasks_store
from graph import BudgetExceeded, add_instruction, build_sprint_graph
import graph as workflow_graph
from llm import chat_json, chat_text, llm_available, stream_chat
from roles import ROSTER, roster_name

BASE_SCENE_PACE = llm.SCENE_PACE
FAST_SCENE_PACE = 0.12

running = False
sprint_task: asyncio.Task | None = None
graph = build_sprint_graph()
agent_config.apply_saved_names()  # 恢复用户保存的成员姓名

# 启动时恢复上一次 Sprint 的看板与产物，刷新/重启后不再“失忆”
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
    await asyncio.to_thread(archive_layout.migrate)
    await asyncio.to_thread(file_knowledge.index_existing)
    if sprint_meta.get("id"):
        artifact_store.set_sprint(sprint_meta["id"])
    yield
    if sprint_task and not sprint_task.done():
        sprint_task.cancel()


app = FastAPI(title="AI 敏捷开发办公室", lifespan=lifespan)

from skills_api import router as skills_router
app.include_router(skills_router)

# 仅允许本机开发/预览前端跨域访问，避免任意站点调用本机 API
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


class SprintRequest(BaseModel):
    requirement: str
    upload_ids: list[str] = []
    fast: bool = False
    save_dir: str = ""  # 界面「保存位置」显式选择；为空则继续从需求文本解析
    budget: float = 0.0  # 单轮花费上限（元），0 = 不限；超限自动收尾停止继续调用模型
    task_kind: str = "auto"
    execution_mode: str = "cloud"
    execution_style: str = "collaborative"


def _local_model_ready() -> bool:
    """Local mode is allowed only when every configured model route stays on loopback."""
    configured = 0
    for roster in ROSTER:
        for config in (agent_config.resolve_llm(roster), agent_config.resolve_llm_backup(roster)):
            if not config.get("api_key"):
                continue
            configured += 1
            host = urlsplit(config.get("base_url") or llm.DEFAULT_BASE_URL).hostname
            if host not in ("localhost", "127.0.0.1", "::1"):
                return False
    return configured > 0


@app.get("/api/privacy/capabilities")
async def privacy_capabilities():
    return {"local_model_ready": _local_model_ready(), "message": "本地模式要求所有已配置的主、备用模型 API 位于本机回环地址。"}


@app.post("/api/sprint/plan")
async def preview_sprint_plan(req: SprintRequest):
    requirement = req.requirement.strip()
    if not requirement:
        raise HTTPException(400, "请先描述需求")
    if req.task_kind not in ("auto", "file", "software"):
        raise HTTPException(400, "任务类型无效")
    if req.execution_mode not in ("cloud", "local"):
        raise HTTPException(400, "执行模式无效")
    if req.execution_style not in ("collaborative", "quick"):
        raise HTTPException(400, "协作模式无效")
    if req.execution_style == "quick" and (req.task_kind == "software" or _detect_file_kind(requirement) == "code"):
        raise HTTPException(400, "快速生成仅支持文件任务")
    direct = req.execution_style == "quick"
    if req.task_kind == "file" and _detect_file_kind(requirement) not in ("docx", "pptx", "xlsx", "pdf", "html"):
        raise HTTPException(400, "文件任务请在需求中写明 Word、PPT、Excel、PDF 或 HTML")
    if req.execution_mode == "local" and not _local_model_ready():
        raise HTTPException(400, "本地模式需要先把所有已配置的主、备用模型 API 改为本机地址")
    uploads_by_id = {item["id"]: item for item in uploads.list_uploads()}
    if any(uid not in uploads_by_id for uid in req.upload_ids):
        raise HTTPException(400, "部分附件已不存在，请移除后重试")
    attachments = [uploads_by_id[uid] for uid in req.upload_ids if uid in uploads_by_id]
    return {
        "route": "直接生成并验收文件" if direct else "需求拆解 → 分工开发 → 测试评审 → 交付",
        "output": {"docx": "Word 文档", "pptx": "PPT 演示文稿", "xlsx": "Excel 表格", "pdf": "PDF 文档"}.get(_detect_file_kind(requirement), "文件") if direct else "代码包及过程文档",
        "attachments": attachments,
        "save_dir": req.save_dir or "默认交付目录",
        "budget": req.budget,
        "execution_mode": req.execution_mode,
        "execution_style": req.execution_style,
        "data_notice": "附件内容将发送到已配置的模型 API" if req.execution_mode == "cloud" else "仅调用本机回环地址的模型 API；请确认该服务自身不会转发数据",
    }


async def run_sprint(
    requirement: str,
    uploads_text: str = "",
    fast: bool = False,
    uploads_images: list | None = None,
    direct_artifact: bool = False,
    upload_ids: list[str] | None = None,
    task_kind: str = "auto",
) -> None:
    global running, sprint_task
    sprint_id = sprint_meta["id"]
    stop_calls = threading.Event()
    budget = float(sprint_meta.get("budget") or 0)
    baseline = float(sprint_meta.get("usage_baseline") or 0)

    def guard_call():
        while True:
            if sprint_meta.get("execution_mode") == "local" and not _local_model_ready():
                raise RuntimeError("本地模式的模型配置已变更，已阻止后续模型调用")
            if stop_calls.is_set():
                raise RuntimeError("本轮迭代已结束，停止后续模型调用")
            if budget and max(0.0, llm.usage_total_cost() - baseline) >= budget:
                raise BudgetExceeded("已达到本轮预算，停止后续模型调用")
            if not bus.is_paused():
                return
            time.sleep(.2)

    call_token = llm.set_call_guard(guard_call)

    async def cost_ticker() -> None:
        """运行期间每 8 秒推送一次增量花费，让用户随时看到成本"""
        try:
            while True:
                await asyncio.sleep(8)
                cur_cost = max(0.0, round(
                    llm.usage_total_cost() - sprint_meta["usage_baseline"], 4
                ))
                sprint_meta["cost"] = cur_cost
                bus.emit(
                    {
                        "type": "sprint_usage",
                        "cost": cur_cost,
                        "tokens": llm.usage_total_tokens(),
                    }
                )
        except asyncio.CancelledError:
            pass

    ticker_task: asyncio.Task | None = None
    try:
        llm.SCENE_PACE = FAST_SCENE_PACE if fast else BASE_SCENE_PACE
        bus.emit({"type": "sprint_start", "requirement": requirement, "sprint_id": sprint_id})
        ticker_task = asyncio.create_task(cost_ticker())
        if direct_artifact:
            # Spend calls on the requested file and its real QA, not simulated
            # multi-round testing and rework for a single-file request.
            deliver_kind = _detect_file_kind(requirement)
            sprint_meta["deliver_kind"] = deliver_kind or ""
            sprint_meta["stage"] = "直接生成成品"
            storage.update_sprint(sprint_id, stage="直接生成成品", deliver_kind=deliver_kind or "")
            bus.emit({"type": "stage", "stage": "直接生成成品", "started_at": sprint_meta["started_at"], "stage_started": time.time()})
            await _generate_document(requirement, upload_ids, sprint_id)
            items = storage.list_artifacts(sprint_id)
            failed = not items or any(a.get("degraded") or a.get("quality_status") == "failed" for a in items)
            pending_checks = not failed and any(a.get("quality_status") != "passed" for a in items)
            accepted = not failed and not pending_checks
            completion_stage = "已完成" if accepted else "交付待验证" if pending_checks else "交付待修正"
            sprint_meta.update(status="completed" if accepted or pending_checks else "failed", stage=completion_stage)
            storage.update_sprint(sprint_id, status=sprint_meta["status"], stage=completion_stage,
                                  deliver_kind=deliver_kind or "", finished_at=time.time())
            bus.emit({"type": "sprint_done", "tasks": [], "accepted": accepted, "pending_checks": pending_checks})
            bus.emit({"type": "stage", "stage": completion_stage, "started_at": sprint_meta["started_at"], "stage_started": time.time()})
            return
        result = await graph.ainvoke(
            {
                "requirement": requirement,
                "requirement_contract": storage.get_sprint(sprint_id).get("contract", {}),
                "tasks": [],
                "rework_count": 0,
                "review_note": "",
                "deliver_kind": "",
                "task_kind": task_kind,
                "uploads_text": uploads_text,
                "uploads_images": uploads_images or [],
            }
        )
        deliver_kind = result.get("deliver_kind", "") if isinstance(result, dict) else ""
        sprint_meta["deliver_kind"] = deliver_kind or sprint_meta.get("deliver_kind", "")
        final_items = [a for a in artifacts_store if a.get("kind") == "final"]
        accepted = bool(final_items) and all(not a.get("degraded") and a.get("quality_status") == "passed" for a in final_items)
        pending_checks = bool(final_items) and not any(a.get("degraded") or a.get("quality_status") == "failed" for a in final_items) and not accepted
        completion_stage = "已完成" if accepted else "交付待验证" if pending_checks else "交付待修正"
        sprint_meta["status"] = "completed" if accepted or pending_checks else "failed"
        sprint_meta["stage"] = completion_stage
        storage.update_sprint(
            sprint_id,
            status=sprint_meta["status"],
            stage=completion_stage,
            deliver_kind=sprint_meta["deliver_kind"],
            finished_at=time.time(),
        )
        bus.emit(
            {
                "type": "stage",
                "stage": completion_stage,
                "started_at": sprint_meta["started_at"],
                "stage_started": time.time(),
            }
        )
    except BudgetExceeded as error:
        # 成本达上限：按已完成收尾，但明确告知是预算熔断而非自然完成
        message = str(error)
        print(f"[Sprint] 预算熔断：{message}")
        sprint_meta["status"] = "completed"
        sprint_meta["stage"] = "已达预算上限"
        storage.update_sprint(
            sprint_id,
            status="completed",
            stage="已达预算上限",
            deliver_kind=sprint_meta.get("deliver_kind", ""),
            finished_at=time.time(),
        )
        bus.emit({"type": "budget_exceeded", "message": message})
        bus.emit(
            {
                "type": "stage",
                "stage": "已达预算上限",
                "started_at": sprint_meta["started_at"],
                "stage_started": time.time(),
            }
        )
    except asyncio.CancelledError:
        sprint_meta["status"] = "stopped"
        storage.update_sprint(sprint_id, status="stopped", stage="已手动结束", finished_at=time.time())
        raise
    except Exception as error:  # noqa: BLE001 —— 图内异常不能拖垮服务
        print(f"[Sprint] 执行异常：{error}")
        sprint_meta["status"] = "failed"
        storage.update_sprint(sprint_id, status="failed", finished_at=time.time())
        bus.emit({"type": "sprint_error", "message": str(error)})
    finally:
        stop_calls.set()
        llm.reset_call_guard(call_token)
        if ticker_task:
            ticker_task.cancel()
        running = False
        sprint_meta["finished_at"] = sprint_meta["finished_at"] or time.time()
        llm.SCENE_PACE = BASE_SCENE_PACE
        forge.set_explicit_save_dir("")  # 清除界面选择的保存位置覆盖
        # 本轮 Sprint 的真实花费/用量落库（含手动结束与异常），历史列表可长期回看
        sprint_calls = max(0, llm.usage_total_calls() - sprint_meta.get("usage_baseline_calls", 0))
        sprint_tokens = max(0, llm.usage_total_tokens() - sprint_meta.get("usage_baseline_tokens", 0))
        sprint_cost = round(llm.usage_total_cost() - sprint_meta["usage_baseline"], 4)
        sprint_meta["cost"] = max(0.0, sprint_cost)
        storage.update_sprint(
            sprint_id,
            usage_calls=sprint_calls,
            usage_tokens=sprint_tokens,
            usage_cost=max(0.0, sprint_cost),
        )
        bus.emit(
            {
                "type": "sprint_usage",
                "cost": sprint_cost,
                "tokens": llm.usage_total_tokens(),
            }
        )


@app.post("/api/sprint/start")
async def start_sprint(req: SprintRequest):
    global running, sprint_task
    requirement = req.requirement.strip()
    if not requirement:
        raise HTTPException(status_code=400, detail="requirement 不能为空")
    if running:
        raise HTTPException(status_code=409, detail="Sprint 正在进行中")
    if req.task_kind not in ("auto", "file", "software"):
        raise HTTPException(400, "任务类型无效")
    if req.execution_mode not in ("cloud", "local"):
        raise HTTPException(400, "执行模式无效")
    if req.execution_style not in ("collaborative", "quick"):
        raise HTTPException(400, "协作模式无效")
    if req.execution_mode == "local" and not _local_model_ready():
        raise HTTPException(400, "本地模式需要先把所有已配置的主、备用模型 API 改为本机地址")
    budget = max(0.0, float(req.budget or 0))
    if budget and budget < 0.01:
        raise HTTPException(status_code=400, detail="预算上限至少 0.01 元，或留 0 表示不限")
    if not llm_available():
        raise HTTPException(
            status_code=400,
            detail="尚未配置任何成员的专属 API，AI 无法开工：请先在「智能体 → ⚙ 设置」中为成员填入 API Key",
        )

    if req.execution_style == "quick" and (req.task_kind == "software" or _detect_file_kind(requirement) == "code"):
        raise HTTPException(400, "快速生成仅支持文件任务")
    direct_artifact = req.execution_style == "quick"
    if req.task_kind == "file" and _detect_file_kind(requirement) not in ("docx", "pptx", "xlsx", "pdf", "html"):
        raise HTTPException(400, "文件任务请在需求中写明 Word、PPT、Excel、PDF 或 HTML")

    # 每轮 Sprint 独立入库，历史产物与代码不再被覆盖
    sprint_id = storage.create_sprint(requirement, budget=budget)
    storage.update_sprint(sprint_id, execution_style=req.execution_style,
                          execution_mode=req.execution_mode, budget=budget)
    # 界面显式选择的保存位置：归一化为绝对路径，优先级高于需求文本解析
    save_dir = os.path.expandvars(os.path.expanduser(req.save_dir.strip()))
    if save_dir:
        try:
            os.makedirs(save_dir, exist_ok=True)
        except OSError as error:
            raise HTTPException(status_code=400, detail=f"保存位置不可用：{error}")
    forge.set_explicit_save_dir(save_dir)
    sprint_meta.update(
        {
            "id": sprint_id,
            "requirement": requirement,
            "started_at": time.time(),
            "finished_at": 0.0,
            "status": "running",
            "stage": "直接生成成品" if direct_artifact else "准备开工",
            "stage_started": time.time(),
            "deliver_kind": "",
            "usage_baseline": llm.usage_total_cost(),
            "usage_baseline_calls": llm.usage_total_calls(),
            "usage_baseline_tokens": llm.usage_total_tokens(),
            "fast": bool(req.fast),
            "budget": budget,
            "execution_mode": req.execution_mode,
            "execution_style": req.execution_style,
        }
    )
    tasks_store.clear()
    bus.event_log.clear()  # 新 Sprint 开启新的事件时间线（历史已落库可查）
    bus.set_paused(False)  # 新任务自动解除暂停闸门，避免一开始就被挂起
    artifact_store.set_sprint(sprint_id)
    forge.begin_sprint(sprint_id)

    running = True
    uploads_text = file_knowledge.context_for_task(requirement, req.upload_ids)
    preliminary_contract = build_contract(
        requirement, uploads_text,
        deliver_kind=_detect_file_kind(requirement) if direct_artifact else "",
    )
    storage.save_contract(sprint_id, preliminary_contract)
    uploads_images = uploads.extract_images(req.upload_ids)
    sprint_task = asyncio.create_task(
        run_sprint(requirement, uploads_text, req.fast, uploads_images, direct_artifact, req.upload_ids, req.task_kind)
    )
    return {"ok": True, "sprint_id": sprint_id, "requirement": requirement, "uploads": len(req.upload_ids)}


@app.post("/api/demo/start")
async def start_demo():
    """零成本演示模式：脚本化回放一场完整 Sprint，不消耗任何 API 额度（无需配置 Key）"""
    global running, sprint_task
    if running:
        raise HTTPException(status_code=409, detail="Sprint 正在进行中")
    running = True
    sprint_task = asyncio.create_task(_run_demo())
    return {"ok": True}


async def _run_demo() -> None:
    global running
    try:
        await demo.run_demo_sprint()
    except asyncio.CancelledError:
        sprint_meta["status"] = "stopped"
        if sprint_meta.get("id"):
            storage.update_sprint(
                sprint_meta["id"], status="stopped",
                stage="已手动结束", finished_at=time.time(),
            )
    except Exception as error:  # noqa: BLE001 —— 演示异常不能拖垮服务
        print(f"[Demo] 演示异常：{error}")
        sprint_meta["status"] = "failed"
        bus.emit({"type": "sprint_error", "message": str(error)})
    finally:
        running = False


@app.post("/api/sprint/stop")
async def stop_sprint():
    """手动结束当前 Sprint：取消工作流，全员转空闲"""
    if not running or not sprint_task or sprint_task.done():
        raise HTTPException(status_code=409, detail="当前没有进行中的任务")
    bus.set_paused(False)  # 先解除暂停闸门，否则取消信号可能进不来
    sprint_task.cancel()
    bus.emit({"type": "sprint_stopped", "message": "任务已手动结束"})
    return {"ok": True}


@app.post("/api/sprint/pause")
async def pause_sprint():
    """真暂停：后端工作流在下一个检查点阻塞，不再产生任何 LLM 调用（省钱）"""
    if not running:
        raise HTTPException(status_code=409, detail="当前没有进行中的任务")
    bus.set_paused(True)
    bus.emit({"type": "sprint_paused", "message": "已暂停：模型调用已全部挂起"})
    return {"ok": True, "paused": True}


@app.post("/api/sprint/resume")
async def resume_sprint():
    """解除暂停闸门，工作流从挂起点继续"""
    bus.set_paused(False)
    bus.emit({"type": "sprint_resumed", "message": "已恢复：团队继续工作"})
    return {"ok": True, "paused": False}


class InstructionRequest(BaseModel):
    text: str


@app.post("/api/sprint/instruct")
async def instruct_sprint(req: InstructionRequest):
    """暂停/运行中补充老板指示：下一处 LLM 调用前自动注入提示词，团队照办"""
    if not running:
        raise HTTPException(status_code=409, detail="当前没有进行中的任务")
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="指示内容不能为空")
    add_instruction(text)
    bus.emit(
        {
            "type": "instruction_added",
            "message": f"收到老板补充指示：{text[:60]}",
            "text": text[:300],
        }
    )
    return {"ok": True}


def _find_task(task_id: str) -> dict:
    for task in tasks_store:
        if task.get("id") == task_id:
            return task
    raise HTTPException(status_code=404, detail="任务不存在")


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消单个任务：不再为其消耗模型配额，看板标记为「已取消」"""
    task = _find_task(task_id)
    if task.get("status") in ("通过", "已取消"):
        raise HTTPException(
            status_code=400, detail=f"任务当前状态为「{task.get('status')}」，不能取消"
        )
    task["status"] = "已取消"
    bus.emit({"type": "task_update", "task": task})
    return {"ok": True, "task": task}


class ReassignRequest(BaseModel):
    roster: int


@app.post("/api/tasks/{task_id}/reassign")
async def reassign_task(task_id: str, req: ReassignRequest):
    """把任务改派给另一位成员，看板与开发环节同步生效"""
    task = _find_task(task_id)
    if req.roster not in ROSTER:
        raise HTTPException(status_code=400, detail="成员不存在")
    if task.get("status") in ("通过", "已取消"):
        raise HTTPException(
            status_code=400, detail=f"任务当前状态为「{task.get('status')}」，不能改派"
        )
    task["assignee_roster"] = req.roster
    bus.emit({"type": "task_update", "task": task})
    return {"ok": True, "task": task}


# ---------------------------------------------------------------------------
# 附件管理：上传文件/文件夹供 AI 加工改造（原件只读，不会被修改）
# ---------------------------------------------------------------------------


@app.post("/api/uploads")
async def upload_files(files: list[UploadFile]):
    saved = []
    skipped = []
    for f in files:
        data = await f.read()
        item = uploads.save_upload(f.filename or "unnamed", data)
        if item:
            await asyncio.to_thread(file_knowledge.index_upload, item["id"], item["name"])
        (saved if item else skipped).append(item or (f.filename or "unnamed"))
    return {"uploads": saved, "skipped_too_large": skipped}


@app.get("/api/uploads")
async def get_uploads():
    return {"uploads": uploads.list_uploads()}


@app.get("/api/uploads/search")
async def search_uploads(q: str = "", ids: str = ""):
    selected = [value for value in ids.split(",") if value] if ids else None
    return file_knowledge.answer(q, selected)


@app.delete("/api/uploads/{upload_id}")
async def remove_upload(upload_id: str):
    if not uploads.delete_upload(upload_id):
        raise HTTPException(status_code=404, detail="附件不存在")
    file_knowledge.remove_upload(upload_id)
    return {"ok": True}


def _sprint_view(sprint_id: str | None) -> dict | None:
    if not sprint_id:
        return None
    return storage.get_sprint(sprint_id)


def _active_round() -> dict:
    """只有正在运行的 Sprint 才算「当前轮次」。

    已结束的 Sprint 属于历史记录（可在「查看记录」里回看），
    不能再回灌到新页面伪装成当前任务，否则用户一打开页面就会看到上一轮的旧任务与旧进度。
    """
    if running:
        return {"tasks": tasks_store, "artifacts": artifacts_store, "sprint": sprint_meta}
    return {"tasks": [], "artifacts": [], "sprint": {}}


@app.get("/api/state")
async def get_state():
    sprint_id = sprint_meta.get("id") if running else None
    baseline = sprint_meta.get("usage_baseline") or 0.0
    active = _active_round()
    return {
        "running": running,
        "paused": bus.is_paused(),
        "llm": llm_available(),
        "tasks": active["tasks"],
        "artifacts": active["artifacts"],
        "sprint": active["sprint"],
        "requirement_contract": (_sprint_view(sprint_id) or {}).get("contract", {}),
        "handoffs": storage.list_handoffs(sprint_id) if sprint_id else [],
        "cost": max(0.0, round(llm.usage_total_cost() - baseline, 4)) if running else 0,
        "history": storage.list_sprints(20),
    }


@app.get("/api/sprints")
async def list_sprints():
    # Keep the fixed quality-baseline runs reachable from the UI history selector.
    return {"sprints": storage.list_sprints(100)}


@app.get("/api/sprints/{sprint_id}")
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


@app.get("/api/artifacts/deliverables")
async def list_deliverables():
    """产物中心使用的跨 Sprint 最终交付物列表。"""
    return {"artifacts": storage.list_deliverables()}


@app.get("/api/sprints/{sprint_id}/artifacts.zip")
async def download_sprint_artifacts_zip(sprint_id: str):
    """把该 Sprint 全部真实文件产物打成一个 zip 一次下载"""
    sprint = storage.get_sprint(sprint_id)
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint 不存在")
    seen: set[str] = set()
    files: list[str] = []
    for artifact in storage.list_artifacts(sprint_id):
        name = artifact.get("file_name") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        path = forge.safe_deliverable_path(name)
        if path and os.path.isfile(path):
            files.append(path)
    if not files:
        raise HTTPException(status_code=404, detail="该 Sprint 没有可下载的真实文件产物")
    download_name = forge.semantic_filename(sprint["requirement"], "zip", "本轮产物")
    zip_name = await asyncio.to_thread(_zip_files, files, download_name)
    return FileResponse(
        forge.output_path(zip_name),
        filename=download_name,
    )


def _zip_files(paths: list[str], zip_name_hint: str) -> str:
    """把若干真实文件打成 zip（文件名维度去重），返回 zip 文件名"""
    zip_name = forge._unique_name(zip_name_hint)
    zip_path = forge.output_path(zip_name)
    used: set[str] = set()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in paths:
            base = os.path.basename(path)
            arc = base
            index = 2
            while arc in used:
                stem, dot, ext = base.rpartition(".")
                arc = f"{stem}({index}){dot}{ext}" if dot else f"{base}({index})"
                index += 1
            used.add(arc)
            zf.write(path, arc)
    return zip_name


@app.delete("/api/sprints/{sprint_id}")
async def delete_sprint(sprint_id: str):
    """删除一条历史 Sprint 及其任务/产物/事件，并清理磁盘上的关联文件与代码目录"""
    if sprint_id == sprint_meta.get("id") and running:
        raise HTTPException(status_code=409, detail="该 Sprint 正在进行中，不能删除")
    if not storage.get_sprint(sprint_id):
        raise HTTPException(status_code=404, detail="Sprint 不存在")
    files = storage.delete_sprint(sprint_id)
    removed = 0
    for name in files:
        path = forge.safe_deliverable_path(name)
        if path:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
    # 该 Sprint 的代码目录一并清理
    code_dir = forge.sprint_code_dir(sprint_id)
    if os.path.isdir(code_dir):
        import shutil as _shutil

        _shutil.rmtree(code_dir, ignore_errors=True)
    # 删除的若是当前驻留 Sprint，清空看板避免悬空引用
    if sprint_id == sprint_meta.get("id"):
        tasks_store.clear()
        artifact_store.clear_current_sprint()
        bus.event_log.clear()
        sprint_meta.update({"id": "", "requirement": "", "status": "idle", "stage": ""})
    return {"ok": True, "removed_files": removed}


@app.get("/api/search")
async def global_search(q: str = ""):
    """全局搜索：覆盖历史需求、任务、产物与真实文件名（不限于当前 Sprint）"""
    keyword = q.strip()
    if not keyword:
        return {"sprints": [], "tasks": [], "artifacts": []}
    return storage.search(keyword)


@app.get("/api/locations")
async def list_locations():
    """真实系统目录（桌面/文档/下载等），供新建任务弹窗「保存位置」下拉选择"""
    return {"locations": forge.list_known_locations()}


@app.get("/api/events")
async def get_events():
    return {"events": bus.event_log}


# ---------------------------------------------------------------------------
# 系统资源（真实采集，前端 3 秒轮询）
# ---------------------------------------------------------------------------
_net_last = {"ts": time.time(), "bytes": 0}


@app.get("/api/system")
async def system_stats():
    global _net_last
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory().percent
    io = psutil.net_io_counters()
    now = time.time()
    total = io.bytes_sent + io.bytes_recv
    elapsed = max(now - _net_last["ts"], 1e-6)
    rate_kb_s = max(total - _net_last["bytes"], 0) / elapsed / 1024
    _net_last = {"ts": now, "bytes": total}
    return {
        "cpu": round(cpu, 1),
        "mem": round(mem, 1),
        "net_kb_s": round(rate_kb_s, 1),
    }


# ---------------------------------------------------------------------------
# 产物中心：老板直接让 AI 生成交付物，成品实时出现在产物中心
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    prompt: str
    upload_ids: list[str] = []


def _fallback_document(prompt: str) -> dict:
    """未配置 LLM 时的兜底文档，保证离线也能演示完整流程"""
    title = prompt[:18] or "AI 生成文档"
    return {
        "title": title,
        "content": (
            f"# {title}\n\n"
            f"> 需求描述：{prompt}\n\n"
            "## 说明\n\n"
            "本次没有获得可用的模型响应，因此保存了模板说明。"
            "请在「智能体 → ⚙ 设置」中检查执行成员的 API 地址、密钥和模型，然后重试。\n\n"
            "## 建议结构\n\n"
            "1. 背景与目标\n"
            "2. 方案设计\n"
            "3. 实施步骤\n"
            "4. 风险与对策\n"
        ),
    }


def _extract_title(content: str, prompt: str) -> str:
    """从 Markdown 首个一级标题提取文档标题，取不到则用需求前 18 字"""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and len(stripped) > 2:
            return stripped[2:].strip()[:30]
    return prompt[:18] or "AI 生成文档"


def _detect_file_kind(prompt: str) -> str | None:
    """根据老板指令判断要生成的真实文件类型；返回 None 表示保持 Markdown"""
    text = prompt.lower()
    if any(w in text for w in ("html", "网页", "svg", "canvas", "h5", "前端页面")):
        return "html"
    if any(word in text for word in (
        "代码包", "代码项目", "源代码", "编程", "写一个程序", "写个程序",
        "开发一个", "开发个", "python脚本", "python 脚本", "软件系统",
        "接口开发", "后端", "前端", "数据库", "api", "应用", "网站",
        "命令行", "cli", "小工具", "脚本",
    )):
        return "code"
    if "pdf" in text and any(word in text for word in ("word", "docx")):
        return "docx"
    if "pdf" in text:
        return "pdf"
    if any(w in text for w in ("ppt", "幻灯", "演示文稿", "slides")):
        return "pptx"
    if any(w in text for w in ("excel", "表格", "xlsx", "预算表", "统计表")):
        return "xlsx"
    if any(w in text for w in ("word", "docx", "文档")):
        return "docx"
    if any(w in text for w in ("markdown", ".md", "md 文档")):
        return None
    # 默认给真实 Word 文档：老板要"能干活"，成品优先给可直接打开的文件
    return "docx"


def _select_artifact_roster(kind: str | None) -> int | None:
    """将直生成品交给有 API 的团队成员，确保调用归属可追踪。"""
    preferred = 5 if kind == "code" else 1
    configured = [roster for roster in sorted(ROSTER) if llm_available(roster)]
    healthy = [roster for roster in configured if not llm.agent_failure(roster)]
    if preferred in healthy:
        return preferred
    return next(iter(healthy or configured), None)


def _should_direct_artifact_sprint(requirement: str) -> bool:
    """Route a clear single-file request straight to generation and real QA."""
    text = requirement.lower()
    explicit_file = any(word in text for word in (
        "ppt", "幻灯", "演示文稿", "slides", "excel", "xlsx", "表格",
        "word", "docx", "文档", "pdf",
    ))
    collaborative = any(word in text for word in (
        "敏捷", "sprint", "团队协作", "任务拆解", "任务分工", "多人协作",
        "完整项目", "代码项目", "源代码", "编程", "写一个程序", "开发一个系统",
        "开发网站", "开发应用", "软件系统", "接口开发",
    ))
    return explicit_file and not collaborative and _detect_file_kind(requirement) in ("docx", "pptx", "xlsx", "pdf", "html")


_KIND_DEFAULT_NAME = {"docx": "文档.docx", "pptx": "演示文稿.pptx", "xlsx": "表格.xlsx", "pdf": "文档.pdf", "code": "项目.zip", "html": "页面.html"}


def _saved_note(dest: str | None, target_dir: str | None = None) -> str:
    if dest:
        return f"\n\n📁 已按你的要求保存到：{dest}"
    if target_dir:
        return f"\n\n⚠ 保存到「{target_dir}」失败：目录不可写或不存在，成品仍可在产物中心下载"
    return ""


def _current_sprint_id() -> str:
    return sprint_meta.get("id") or ""


async def _generate_document_safe(prompt: str, upload_ids: list[str] | None = None, sprint_id: str | None = None) -> None:
    """兜住生成过程中的任何异常：失败也落一条带警示的产物，不让用户空等"""
    sid = sprint_id or _current_sprint_id()
    initial = storage.get_sprint(sid) if sid else None
    direct = initial and str(initial.get("stage", "")).startswith("直接生成成品")
    try:
        await _generate_document(prompt, upload_ids, sprint_id)
    except Exception as error:  # noqa: BLE001
        print(f"[Generate] 生成失败：{error}")
        add_artifact(
            kind="custom",
            title="生成失败",
            sprint_id=sprint_id,
            source="fallback",
            quality_status="failed",
            author_roster=None,
            content=(
                f"# 生成失败\n\n需求：{prompt}\n\n错误：{error}\n\n"
                "可点击「重新生成」重试，或检查成员 API 配置后重试。"
            ),
            degraded=True,
            degraded_reason=str(error)[:160],
            regen={"mode": "custom", "prompt": prompt, "upload_ids": upload_ids or []},
        )
    finally:
        if direct:
            items = storage.list_artifacts(sid)
            failed = not items or any(item.get("degraded") or item.get("quality_status") == "failed" for item in items)
            unchecked = any(item.get("quality_status") != "passed" for item in items)
            status = "failed" if failed else "completed"
            stage = "生成失败或验收未通过" if failed else "文件已生成，部分验收待完成" if unchecked else "成品验收完成"
            storage.update_sprint(sid, status=status, stage=stage, finished_at=time.time())
            if sprint_meta.get("id") == sid and not running:
                sprint_meta.update(status=status, stage=stage, finished_at=time.time())
            bus.emit({"type": "generate_progress", "stage": stage, "prompt": prompt[:40], "sprint_id": sid})


async def _generate_document(prompt: str, upload_ids: list[str] | None = None, sprint_id: str | None = None) -> None:
    """生成期间只将实际负责的成员标记为工作中，完成后恢复空闲。"""
    kind = _detect_file_kind(prompt)
    roster = _select_artifact_roster(kind) or (5 if kind == "code" else 1)
    bus.emit({
        "type": "set_state",
        "rosterNo": roster,
        "state": "working",
        "task": f"直接生成：{prompt[:36]}",
        "source": "artifact_generation",
    })
    try:
        await _generate_document_impl(prompt, upload_ids, sprint_id, roster)
    finally:
        bus.emit({
            "type": "set_state",
            "rosterNo": roster,
            "state": "idle",
            "task": "等待新任务",
            "source": "artifact_generation",
        })


async def _generate_document_impl(
    prompt: str,
    upload_ids: list[str] | None = None,
    sprint_id: str | None = None,
    assigned_roster: int | None = None,
) -> None:
    """后台生成真实交付物文件并入库（自动经 WebSocket 推送 artifact_new）"""
    # 极简需求直通：「生成空文件」类指令无需 LLM，直接落盘并复制到目标位置
    if "空文件" in prompt:
        file_name = forge._unique_name("空文件.txt")
        with open(forge.output_path(file_name), "w", encoding="utf-8") as f:
            f.write("")
        target_dir = forge.extract_target_dir(prompt)
        dest = (
            await asyncio.to_thread(forge.copy_to_target, file_name, target_dir)
            if target_dir
            else None
        )
        add_artifact(
            kind="custom",
            sprint_id=sprint_id,
            title="空文件.txt",
            source="local",
            author_roster=None,
            content=f"已生成空文件：{file_name}" + _saved_note(dest, target_dir),
            file_name=file_name,
            regen={"mode": "custom", "prompt": prompt, "upload_ids": upload_ids or []},
        )
        return

    kind = _detect_file_kind(prompt)
    roster = assigned_roster if assigned_roster is not None else _select_artifact_roster(kind)
    attachments = file_knowledge.context_for_task(prompt, upload_ids or [])
    spec = (
        f"以下附件内容作为素材，请基于它们加工改造：\n\n{attachments}\n\n需求：{prompt}"
        if attachments else prompt
    )

    if kind == "code":
        sid = sprint_id or _current_sprint_id()
        code_dir = forge.sprint_code_dir(sid)
        bus.emit({"type": "generate_progress", "stage": "规划并整合完整项目", "prompt": prompt[:40]})
        report = await asyncio.to_thread(forge.create_project, spec, code_dir, roster, prompt)
        filename = await asyncio.to_thread(_zip_code_for, sid, prompt)
        target_dir = forge.extract_target_dir(prompt)
        saved = await asyncio.to_thread(forge.copy_to_target, filename, target_dir) if filename and target_dir else None
        add_artifact(kind="custom", title=prompt[:30] + "（代码项目）", author_roster=roster,
                     sprint_id=sid, content=forge.self_check_note(report) + _saved_note(saved, target_dir), file_name=filename,
                     quality_status=report["status"], degraded=not filename or report["status"] == "failed",
                     degraded_reason="项目检查未通过" if report["status"] == "failed" else "",
                     regen={"mode": "code", "prompt": prompt, "spec": spec, "target_dir": forge.extract_target_dir(prompt) or ""})
        return

    if kind:
        title = forge.semantic_stem(prompt, _KIND_DEFAULT_NAME[kind].rpartition('.')[0])
        filename = forge.semantic_filename(prompt, kind, _KIND_DEFAULT_NAME[kind].rpartition('.')[0])
        bus.emit({"type": "generate_progress", "stage": "正在撰写成品内容", "prompt": prompt[:40]})
        file_name = await asyncio.to_thread(
            forge.forge_file, kind, spec, filename, roster, None, True
        )
        if file_name:
            bus.emit({"type": "generate_progress", "stage": "正在校验与自检成品", "prompt": prompt[:40]})
            preview = await asyncio.to_thread(forge.extract_preview, file_name)
            target_dir = forge.extract_target_dir(prompt)
            dest = (
                await asyncio.to_thread(forge.copy_to_target, file_name, target_dir)
                if target_dir
                else None
            )
            check = await asyncio.to_thread(forge.self_check, file_name, kind)
            check_note = forge.self_check_note(check)
            check_failed = check.get("status", "passed" if check.get("ok") else "failed") == "failed"
            add_artifact(
                kind="custom",
                sprint_id=sprint_id,
                title=f"{title}（{_KIND_DEFAULT_NAME[kind].rpartition('.')[2]}）",
                source="model",
                quality_status=check.get("status", "passed" if check.get("ok") else "failed"),
                author_roster=roster,
                content=(
                    preview
                    or f"已生成真实 {kind} 文件：{file_name}\n\n> 需求描述：{prompt}"
                ) + _saved_note(dest, target_dir) + check_note,
                file_name=file_name,
                degraded=check_failed,
                degraded_reason="成品自检未全部通过，成品可能不完整，建议重新生成" if check_failed else "",
                regen={
                    "mode": "forge",
                    "file_kind": kind,
                    "spec": spec,
                    "filename": filename,
                    "roster": roster,
                    "target_dir": target_dir or "",
                },
            )
            return
        # forge 失败：落到下方 Markdown 兜底，保证流程不中断，但明确标记为兜底产物

    meta: dict = {}
    content = await asyncio.to_thread(
        chat_text,
        "你是 AI 办公室的文档助手。根据老板的要求生成一份完整、详实、可直接使用的交付物文档。"
        "要求：Markdown 格式；第一行是「# 文档标题」；用二级/三级标题分节，结构清晰、内容充实、贴近实际工作产出。"
        "如果提供了附件内容，必须基于附件内容加工改造。",
        [{"role": "user", "content": spec}],
        fallback=lambda: _fallback_document(prompt)["content"],
        temperature=0.7,
        max_tokens=4000,
        timeout=300,
        roster=roster,
        meta=meta,
    )
    if not content or not content.strip():
        content = _fallback_document(prompt)["content"]
    reason = (
        f"{kind} 文件生成失败，已回退为 Markdown 摘要"
        if kind
        else meta.get("fallback_reason", "")
    )
    add_artifact(
        kind="custom",
        sprint_id=sprint_id,
        title=_extract_title(content, prompt),
        source="fallback" if reason else "model",
        quality_status="failed" if kind else "unchecked",
        author_roster=roster,
        content=content,
        degraded=bool(reason),
        degraded_reason=reason,
        regen={
            "mode": "forge" if kind else "custom",
            "file_kind": kind,
            "spec": spec,
            "prompt": prompt,
            "filename": forge.semantic_filename(prompt, kind, _KIND_DEFAULT_NAME[kind].rpartition('.')[0]) if kind else "",
            "upload_ids": upload_ids or [],
            "roster": roster,
        },
    )


def _zip_code_for(sprint_id: str, requirement: str = "") -> str | None:
    """把指定 Sprint 的代码目录打成 zip，返回文件名；无代码时返回 None"""
    code_dir = forge.selected_project(forge.sprint_code_dir(sprint_id))
    if not os.path.isdir(code_dir):
        return None
    files = [os.path.join(root, f) for root, _, fs in os.walk(code_dir) for f in fs]
    if not files:
        return None
    if not requirement:
        requirement = (storage.get_sprint(sprint_id) or {}).get("requirement", "")
    zip_name = forge._unique_name(forge.semantic_filename(requirement, "zip", "代码项目"))
    zip_path = forge.output_path(zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for full in files:
            zf.write(full, os.path.relpath(full, code_dir))
    import artifact_quality
    artifact_quality.save_report(zip_path, forge.WORKSPACE_DIR, forge.project_report(forge.sprint_code_dir(sprint_id)))
    return zip_name


@app.get("/api/artifacts/code.zip")
async def download_code_zip(sprint: str | None = None):
    """打包下载指定 Sprint（默认当前）产出的全部代码"""
    sprint_id = sprint or _current_sprint_id()
    zip_name = await asyncio.to_thread(_zip_code_for, sprint_id)
    if not zip_name:
        raise HTTPException(status_code=404, detail="该 Sprint 尚未产出代码")
    return FileResponse(
        forge.output_path(zip_name), filename=os.path.basename(zip_name)
    )


def _find_artifact(artifact_id: str) -> dict:
    artifact = storage.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="产物不存在")
    return artifact


@app.get("/api/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: str):
    """下载产物对应的真实文件（仅允许 deliverables 目录内）"""
    artifact = _find_artifact(artifact_id)
    if not artifact.get("file_name"):
        raise HTTPException(status_code=404, detail="该产物没有关联的真实文件")
    path = forge.safe_deliverable_path(artifact["file_name"])
    if not path:
        raise HTTPException(status_code=404, detail="文件不存在或已被清理")
    return FileResponse(path, filename=os.path.basename(path))


@app.post("/api/artifacts/{artifact_id}/reveal")
async def reveal_artifact(artifact_id: str):
    """在系统文件管理器中定位产物文件（仅本机自用场景）"""
    artifact = _find_artifact(artifact_id)
    if not artifact.get("file_name"):
        raise HTTPException(status_code=404, detail="该产物没有关联的真实文件")
    path = forge.safe_deliverable_path(artifact["file_name"])
    if not path:
        raise HTTPException(status_code=404, detail="文件不存在或已被清理")
    if os.name == "nt":
        import subprocess

        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    return {"ok": True, "path": path}


@app.get("/api/artifacts/{artifact_id}/preview", response_class=HTMLResponse)
async def preview_artifact(artifact_id: str):
    """Word/PPT/Excel 结构化 HTML 预览：标题层级、分页、工作表表格原样呈现"""
    artifact = _find_artifact(artifact_id)
    file_name = artifact.get("file_name") or ""
    body = forge.preview_html(file_name) if file_name else ""
    if file_name:
        path = forge.safe_deliverable_path(file_name)
        if path:
            import artifact_quality
            report = await asyncio.to_thread(forge.self_check, file_name)
            if report and report.get("pages"):
                body = "".join(f'<section><img alt="第 {i + 1} 页" style="width:100%;max-width:1000px;display:block;margin:16px auto;border:1px solid #ddd" src="/api/artifacts/{artifact_id}/quality/pages/{i + 1}" /></section>' for i in range(len(report["pages"])))
    if not body:
        body = "<p>该产物没有可预览的真实文件（可能为纯文本产物或文件已被清理）。</p>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>body{font-family:'Microsoft YaHei',sans-serif;padding:24px;color:#222;}"
        "h1{font-size:22px;}h2{font-size:18px;margin-top:18px;}h3{font-size:15px;color:#555;}"
        "table{border-collapse:collapse;margin:8px 0;width:100%;}"
        "td{border:1px solid #ccc;padding:4px 8px;font-size:13px;}"
        ".preview-slide{border:1px solid #ddd;border-radius:8px;padding:12px 16px;margin:12px 0;}"
        "</style></head><body>" + body + "</body></html>"
    )


@app.get("/api/artifact-capabilities")
async def artifact_capabilities():
    import office_runtime
    import project_agent
    return {**office_runtime.capabilities(), "code_sandbox": await asyncio.to_thread(project_agent.docker_available)}


@app.get("/api/artifacts/{artifact_id}/provenance")
async def artifact_provenance(artifact_id: str):
    """Trace PO→SM→BE/FE→candidate→QA→LEAD for one saved artifact."""
    import artifact_quality
    import contributions

    artifact = _find_artifact(artifact_id)
    sprint_id = artifact.get("sprint_id") or ""
    sprint = storage.get_sprint(sprint_id) or {}
    is_manual_version = (artifact.get("regen") or {}).get("mode") == "manual_revision"
    candidate_id = (artifact.get("parent_id") if artifact.get("kind") == "final" and not is_manual_version
                    else artifact_id)
    if artifact.get("kind") == "final" and not candidate_id and not artifact.get("file_name"):
        candidates = [entry for entry in storage.list_artifacts(sprint_id) if entry.get("kind") == "candidate"]
        if candidates:
            candidate_id = candidates[-1]["id"]
    candidate = storage.get_artifact(candidate_id) or artifact
    if (candidate.get("regen") or {}).get("source_candidate_id"):
        lead_candidate = storage.get_artifact(candidate["regen"]["source_candidate_id"]) or candidate
    else:
        lead_candidate = candidate

    def saved_report(item: dict, filename: str):
        path = forge.safe_deliverable_path(item.get("file_name") or "")
        if not path:
            return None
        report_file = artifact_quality.report_directory(path, forge.WORKSPACE_DIR) / filename
        try:
            return json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    tasks = storage.list_tasks(sprint_id) if sprint_id else []
    actual_contributions = []
    for task in tasks:
        if not task.get("contribution_ref"):
            continue
        try:
            valid, reason, manifest = contributions.inspect(sprint_id, task["id"])
        except ValueError:
            continue
        preview = ""
        if manifest.get("kind") == "text" and manifest.get("files"):
            try:
                preview = Path(manifest["files"][0]["path"]).read_text(encoding="utf-8")[:1800]
            except OSError:
                pass
        actual_contributions.append({"task_id": task["id"], "roster": manifest.get("roster"),
                                     "role_scope": manifest.get("role_scope"), "valid": valid,
                                     "reason": reason, "files": manifest.get("files", []),
                                     "requirement_ids": manifest.get("requirement_ids", []),
                                     "preview": preview})
    return {
        "execution_style": sprint.get("execution_style", "legacy_unknown"),
        "overall_status": artifact.get("quality_status", "unchecked"),
        "failure_reason": artifact.get("degraded_reason", ""),
        "contract": sprint.get("contract", {}), "tasks": tasks,
        "handoffs": storage.list_handoffs(sprint_id) if sprint_id else [],
        "contributions": actual_contributions,
        "candidate": {"id": candidate.get("id"), "version": candidate.get("version"),
                      "parent_id": candidate.get("parent_id"), "file_name": candidate.get("file_name"),
                      "sha256": (candidate.get("regen") or {}).get("sha256")},
        "qa": saved_report(candidate, "qa-report.json"),
        "lead": saved_report(lead_candidate, "lead-decision.json"),
        "usage": {key: sprint.get(key, 0) for key in ("usage_calls", "usage_tokens", "usage_cost")},
    }


@app.get("/api/artifacts/{artifact_id}/quality")
async def artifact_quality_report(artifact_id: str):
    artifact = _find_artifact(artifact_id)
    file_name = artifact.get("file_name")
    if not file_name:
        raise HTTPException(status_code=404, detail="该产物没有真实文件")
    result = await asyncio.to_thread(forge.self_check, file_name)
    return {key: result.get(key) for key in ("kind", "status", "ok", "checks", "metrics", "review", "attempts", "pages")}


@app.get("/api/artifacts/{artifact_id}/quality/pages/{page}")
async def artifact_quality_page(artifact_id: str, page: int):
    import artifact_quality
    artifact = _find_artifact(artifact_id)
    path = forge.safe_deliverable_path(artifact.get("file_name") or "")
    if not path:
        raise HTTPException(status_code=404, detail="文件不存在")
    report = artifact_quality.load_report(path, forge.WORKSPACE_DIR)
    if not report or page < 1 or page > len(report.get("pages", [])):
        raise HTTPException(status_code=404, detail="该页尚未渲染")
    image = artifact_quality.report_directory(path, forge.WORKSPACE_DIR) / f"page-{page}.png"
    if not image.is_file():
        raise HTTPException(status_code=404, detail="预览图片不存在")
    return FileResponse(image, media_type="image/png")


@app.get("/api/artifacts/{artifact_id}/versions")
async def artifact_versions(artifact_id: str):
    """版本链：返回该产物所在链条的全部版本（根 + 历次修订，含链式挂载）"""
    artifact = _find_artifact(artifact_id)
    sprint_id = artifact.get("sprint_id") or _current_sprint_id()
    root_id = storage.artifact_root(sprint_id, artifact_id)
    chain = storage.artifact_versions(sprint_id, root_id)
    return {"root_id": root_id, "versions": chain}


@app.delete("/api/artifacts/{artifact_id}")
async def delete_artifact(artifact_id: str):
    """删除单条产物及其磁盘文件（修订版本不连带删除）"""
    artifact = _find_artifact(artifact_id)
    file_name = storage.delete_artifact(artifact_id)
    if file_name:
        path = forge.safe_deliverable_path(file_name)
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    with artifact_store._lock:  # 内存快照同步移除
        artifact_store.artifacts_store[:] = [
            item for item in artifact_store.artifacts_store if item["id"] != artifact_id
        ]
    bus.emit({"type": "artifact_deleted", "artifact_id": artifact_id})
    return {"ok": True}


class RevisionRequest(BaseModel):
    instruction: str


class RollbackRequest(BaseModel):
    target_id: str


def _regen_spec(artifact: dict) -> dict:
    regen = artifact.get("regen") or {}
    if not regen:
        raise HTTPException(status_code=400, detail="该产物没有保存生成参数，无法重试")
    return regen


async def _rebuild(artifact: dict, instruction: str = "") -> dict:
    """按产物的 regen 参数重新生成内容/文件，返回可写回字段"""
    regen = _regen_spec(artifact)
    mode = regen.get("mode")
    suffix = f"\n\n修订要求：{instruction}" if instruction else ""

    if mode == "code":
        sprint_id = artifact.get("sprint_id") or _current_sprint_id()
        code_dir = forge.sprint_code_dir(sprint_id)
        if instruction:
            report = await asyncio.to_thread(
                forge.create_project,
                (regen.get("spec") or regen.get("prompt") or artifact.get("content", "")) + suffix,
                code_dir,
                None,
                regen.get("prompt") or (storage.get_sprint(sprint_id) or {}).get("requirement", ""),
            )
        else:
            import project_agent
            previous = forge.project_report(code_dir)
            report = await asyncio.to_thread(project_agent.evaluate, Path(forge.selected_project(code_dir)), previous.get("planned_files", []), previous.get("runtime", "python"))
            (Path(code_dir) / ".project-quality.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        failures = [check["detail"] for check in report["checks"] if check["ok"] is False]
        zip_name = await asyncio.to_thread(_zip_code_for, sprint_id)
        if not zip_name:
            raise HTTPException(status_code=502, detail="当前 Sprint 没有可打包的代码文件")
        saved = ""
        if regen.get("target_dir"):
            dest = await asyncio.to_thread(
                forge.copy_to_target, zip_name, regen["target_dir"]
            )
            saved = _saved_note(dest, regen["target_dir"])
        return {
            "content": f"# 最终交付物\n\n- 代码包：{zip_name}" + forge.self_check_note(report) + saved,
            "file_name": zip_name,
            "degraded": bool(failures),
            "degraded_reason": "；".join(failures[:4]),
            "quality_status": report["status"],
        }

    if mode == "forge" and regen.get("file_kind"):
        spec = f"{regen.get('spec', '')}{suffix}"
        previous_path = forge.safe_deliverable_path(artifact.get("file_name") or "")
        if previous_path:
            import artifact_quality
            directory = artifact_quality.report_directory(previous_path, forge.WORKSPACE_DIR)
            previous = next((path for path in (directory / "source.md", directory / "source.json") if path.is_file()), None)
            if previous:
                spec += "\n\n已有成品（保留正确内容与结构，仅按修订要求修改）：\n" + previous.read_text(encoding="utf-8")[:40000]
        file_name = await asyncio.to_thread(
            forge.forge_file,
            regen["file_kind"],
            spec,
            regen.get("filename") or f"重生成.{regen['file_kind']}",
            regen.get("roster"),
        )
        if not file_name:
            raise HTTPException(
                status_code=502,
                detail="重新生成失败：模型未返回可用脚本或文件生成失败，请稍后重试",
            )
        preview = await asyncio.to_thread(forge.extract_preview, file_name)
        saved = ""
        if regen.get("target_dir"):
            dest = await asyncio.to_thread(
                forge.copy_to_target, file_name, regen["target_dir"]
            )
            saved = _saved_note(dest, regen["target_dir"])
        check = await asyncio.to_thread(forge.self_check, file_name, regen["file_kind"])
        return {
            "content": (preview or f"已重新生成真实文件：{file_name}") + saved + forge.self_check_note(check),
            "file_name": file_name,
            "degraded": check.get("status", "passed" if check["ok"] else "failed") == "failed",
            "degraded_reason": "重新生成的成品仍未通过质检" if check.get("status", "passed" if check["ok"] else "failed") == "failed" else "",
            "source": "model",
            "quality_status": check.get("status", "passed" if check["ok"] else "failed"),
        }

    prompt = (regen.get("prompt") or artifact.get("content", "")) + suffix
    meta: dict = {}
    content = await asyncio.to_thread(
        chat_text,
        "你是 AI 办公室的文档助手。根据要求输出完整、详实的 Markdown 文档，"
        "第一行是「# 标题」，不要只给提纲。",
        [{"role": "user", "content": prompt}],
        fallback=artifact.get("content", ""),
        temperature=0.6,
        max_tokens=4000,
        timeout=300,
        meta=meta,
    )
    if not content.strip():
        raise HTTPException(status_code=502, detail="重新生成失败：模型未返回内容")
    return {
        "content": content,
        "degraded": bool(meta.get("fallback_reason")),
        "degraded_reason": meta.get("fallback_reason", ""),
        "source": "fallback" if meta.get("fallback_reason") else "model",
        "quality_status": "unchecked",
    }


@app.post("/api/artifacts/{artifact_id}/retry")
async def retry_artifact(artifact_id: str):
    """重新生成单个产物（保留同一条记录并刷新内容）"""
    artifact = _find_artifact(artifact_id)
    if artifact.get("kind") == "final" and (storage.get_sprint(artifact.get("sprint_id") or "") or {}).get("execution_style") == "collaborative":
        raise HTTPException(status_code=409, detail="协作成品不能原地重试；请创建新修订版本并重新验收")
    fields = await _rebuild(artifact)
    updated = artifact_store.update_artifact(artifact_id, **fields)
    return {"ok": True, "artifact": updated}


def _manual_contribution_refs(sprint_id: str, artifact: dict) -> list[str]:
    regen = artifact.get("regen") or {}
    if regen.get("contribution_refs"):
        return list(regen["contribution_refs"])
    candidate = storage.get_artifact(regen.get("candidate_id") or "") or {}
    return list((candidate.get("regen") or {}).get("contribution_refs", []))


def _manual_call_guard(sprint_id: str):
    sprint = storage.get_sprint(sprint_id) or {}
    budget = max(0.0, float(sprint.get("budget") or 0))
    spent = max(0.0, float(sprint.get("usage_cost") or 0))
    baseline_cost = llm.usage_total_cost()
    remaining_budget = max(0.0, budget - spent) if budget else 0.0

    def guard():
        if sprint.get("execution_mode") == "local" and not _local_model_ready():
            raise RuntimeError("本地模式模型配置已变更，已阻止修订调用")
        current_delta = max(0.0, llm.usage_total_cost() - baseline_cost)
        if budget and (spent >= budget or current_delta >= remaining_budget):
            raise BudgetExceeded("修订操作已达到 Sprint 剩余预算")

    return llm.set_call_guard(guard)


def _usage_snapshot() -> tuple[int, int, float]:
    return llm.usage_total_calls(), llm.usage_total_tokens(), llm.usage_total_cost()


def _persist_manual_usage_delta(sprint_id: str, before: tuple[int, int, float]) -> None:
    current = _usage_snapshot()
    sprint = storage.get_sprint(sprint_id) or {}
    storage.update_sprint(
        sprint_id,
        usage_calls=int(sprint.get("usage_calls") or 0) + max(0, current[0] - before[0]),
        usage_tokens=int(sprint.get("usage_tokens") or 0) + max(0, current[1] - before[1]),
        usage_cost=round(float(sprint.get("usage_cost") or 0) + max(0.0, current[2] - before[2]), 4),
    )


async def _revalidate_manual_collaborative_version(artifact: dict, sprint_id: str) -> dict:
    """Run a manual collaborative revision through the same QA→LEAD hash gate."""
    import hashlib
    import handoffs
    import review_decision
    import verification

    sprint = storage.get_sprint(sprint_id) or {}
    path = forge.safe_deliverable_path(artifact.get("file_name") or "")
    regen = artifact.get("regen") or {}
    if not path:
        return artifact_store.update_artifact(
            artifact["id"], quality_status="unchecked", degraded_reason="修订版本缺少真实文件，QA/LEAD 未运行"
        )
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest != regen.get("sha256"):
        return artifact_store.update_artifact(
            artifact["id"], quality_status="failed", degraded_reason="修订文件哈希与版本记录不一致"
        )
    if sprint.get("budget") and float(sprint.get("usage_cost") or 0) >= float(sprint.get("budget") or 0):
        return artifact_store.update_artifact(
            artifact["id"], quality_status="unchecked", degraded_reason="Sprint 预算已用完，修订版本等待 QA/LEAD"
        )
    before_usage = _usage_snapshot()
    token = _manual_call_guard(sprint_id)
    try:
        contract = sprint.get("contract") or {}
        refs = [f"requirement:{item['requirement_id']}" for item in contract.get("requirements", [])]
        refs += list(regen.get("contribution_refs") or [])
        offered = handoffs.offer(
            sprint_id, task_id="", from_roster=6, to_roster=4,
            input_refs=refs or [f"contract:{sprint_id}"], output_refs=[f"artifact:{artifact['id']}"],
            claims=[f"手动修订版本 SHA-256 {digest}，请重新逐条验收"],
            acceptance_criteria=["QA 对修订后的真实文件及同一哈希重新核验"],
        )
        if offered.get("status") == "ready":
            handoffs.receive(offered["id"], accepted=True)
        evidence = workflow_graph._workflow_evidence(
            sprint_id, storage.list_tasks(sprint_id), contract, digest,
        )
        report = await asyncio.to_thread(verification.verify, artifact, contract, digest, evidence)
        qa_artifact = add_artifact(
            kind="test", title="手动修订版 QA 逐条验证", author_roster=4,
            content=json.dumps(report, ensure_ascii=False, indent=2),
            quality_status=report.get("status", "unchecked"),
            source="model" if report.get("status") != "unchecked" else "fallback",
            degraded=report.get("status") == "failed", degraded_reason=report.get("reason", ""),
            regen={"mode": "manual_revision_qa", "candidate_id": artifact["id"],
                   "candidate_sha256": digest}, sprint_id=sprint_id,
        )
        lead_offer = handoffs.offer(
            sprint_id, task_id="", from_roster=4, to_roster=5,
            input_refs=[f"contract:{sprint_id}", f"artifact:{artifact['id']}", f"artifact:{qa_artifact['id']}"],
            output_refs=[f"artifact:{qa_artifact['id']}"],
            claims=[f"QA 对修订哈希 {digest} 的结论为 {report.get('status', 'unchecked')}"],
            acceptance_criteria=["LEAD 独立复核相同修订 SHA-256"],
        )
        if lead_offer.get("status") == "ready":
            handoffs.receive(lead_offer["id"], accepted=True)
        records = storage.list_handoffs(sprint_id)
        lead = await asyncio.to_thread(
            review_decision.decide, artifact, contract, report, records, digest, None, evidence,
        )
        add_artifact(
            kind="review", title="手动修订版 LEAD 独立复核", author_roster=5,
            content=json.dumps(lead, ensure_ascii=False, indent=2),
            quality_status=lead.get("status", "unchecked"),
            source="model" if lead.get("status") != "unchecked" else "fallback",
            degraded=lead.get("status") == "failed", degraded_reason=lead.get("reason", ""),
            regen={"mode": "manual_revision_lead", "candidate_id": artifact["id"],
                   "candidate_sha256": digest}, sprint_id=sprint_id,
        )
        verified = (contract.get("status") == "ready" and report.get("status") == "passed"
                    and lead.get("status") == "passed" and lead.get("candidate_sha256") == digest)
        failed = report.get("status") == "failed" or lead.get("status") == "failed"
        status = "passed" if verified else "failed" if failed else "unchecked"
        reason = "" if verified else report.get("reason") or lead.get("reason") or "修订版 QA/LEAD 未全部通过"
        return artifact_store.update_artifact(artifact["id"], quality_status=status,
                                               degraded_reason=reason)
    finally:
        llm.reset_call_guard(token)
        _persist_manual_usage_delta(sprint_id, before_usage)


@app.post("/api/artifacts/{artifact_id}/revise")
async def revise_artifact(artifact_id: str, req: RevisionRequest):
    """基于现有产物继续修改：生成一个新版本，旧版本保留可追溯"""
    instruction = req.instruction.strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="修改要求不能为空")
    artifact = _find_artifact(artifact_id)
    sprint_id = artifact.get("sprint_id") or _current_sprint_id()
    collaborative_final = (artifact.get("kind") == "final" and
                           (storage.get_sprint(sprint_id) or {}).get("execution_style") == "collaborative")
    if collaborative_final and running:
        raise HTTPException(status_code=409, detail="当前协作 Sprint 仍在运行，结束后再修订")
    if collaborative_final and artifact.get("file_name"):
        import candidate_revision
        import hashlib
        kind = Path(artifact["file_name"]).suffix.lower().lstrip(".")
        if kind == "zip":
            kind = "code"
        base_candidate = storage.get_artifact((artifact.get("regen") or {}).get("candidate_id") or "") or {}
        contribution_refs = (base_candidate.get("regen") or {}).get("contribution_refs", [])
        project_dir = ((artifact.get("regen") or {}).get("project_dir") or
                       (base_candidate.get("regen") or {}).get("project_dir", ""))
        actual = await asyncio.to_thread(forge.extract_preview, artifact["file_name"], 14000)
        if kind == "code" and project_dir:
            import project_agent
            actual = project_agent.snapshot(Path(project_dir), limit=20000)
        meta: dict = {}
        before_usage = _usage_snapshot()
        call_token = _manual_call_guard(sprint_id)
        try:
            patch_data = await asyncio.to_thread(
                chat_json,
                "按用户要求只修改一个明确位置，输出 JSON："
                '{"location":"第N页/第N段/工作表!A1/文件路径",'
                '"old_text":"需要替换的精确旧文本","replacement":"完整替换文本或公式或代码文件内容",'
                '"path":"代码相对路径"}。找不到唯一位置时返回空对象。不得删除测试。',
                json.dumps({"instruction": instruction, "kind": kind, "actual_content": actual}, ensure_ascii=False),
                fallback={}, roster=2 if kind == "code" else 3, meta=meta, max_tokens=6500, timeout=180,
            )
        finally:
            llm.reset_call_guard(call_token)
            _persist_manual_usage_delta(sprint_id, before_usage)
        if meta.get("fallback_reason") or not isinstance(patch_data, dict) or not patch_data:
            raise HTTPException(status_code=502, detail="未能定位安全的定点修订位置，原成品未改动")
        file_name, reason, detail = await asyncio.to_thread(
            candidate_revision.apply, sprint_id, kind, artifact["file_name"], patch_data, project_dir
        )
        if not file_name:
            raise HTTPException(status_code=422, detail=reason or "定点修订失败，原成品未改动")
        path = forge.safe_deliverable_path(file_name)
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else ""
        fields = {"content": artifact.get("content", "") + f"\n\n修订要求：{instruction}\n修订位置：{detail}",
                  "file_name": file_name, "quality_status": "unchecked", "source": "model",
                  "regen": {"mode": "manual_revision", "file_kind": kind, "sha256": digest,
                            "project_dir": detail.get("project_dir", project_dir),
                            "contribution_refs": contribution_refs}}
    else:
        fields = await _rebuild(artifact, instruction)
    root_id = storage.artifact_root(sprint_id, artifact_id)
    version = storage.latest_artifact_version(sprint_id, root_id) + 1
    created = add_artifact(
        kind=artifact.get("kind", "custom"),
        title=f"{artifact.get('title', '产物')}（修订 v{version}）",
        author_roster=artifact.get("author_roster"),
        content=fields.get("content", artifact.get("content", "")),
        task_id=artifact.get("task_id"),
        file_name=fields.get("file_name") or artifact.get("file_name"),
        degraded=fields.get("degraded", False),
        degraded_reason=fields.get("degraded_reason", ""),
        source=fields.get("source", artifact.get("source", "unknown")),
        quality_status="unchecked" if collaborative_final else fields.get("quality_status", "unchecked"),
        regen=fields.get("regen", artifact.get("regen")),
        parent_id=artifact_id,
        version=version,
        sprint_id=sprint_id,
    )
    if collaborative_final and not created.get("degraded"):
        created = await _revalidate_manual_collaborative_version(created, sprint_id)
    return {"ok": True, "artifact": created}


@app.post("/api/artifacts/{artifact_id}/rollback")
async def rollback_artifact(artifact_id: str, req: RollbackRequest):
    """回滚到版本链上的某个历史版本：以旧版内容/文件为蓝本生成一个新版本（旧版保留）"""
    artifact = _find_artifact(artifact_id)
    target = _find_artifact(req.target_id)
    sprint_id = artifact.get("sprint_id") or _current_sprint_id()
    root_id = storage.artifact_root(sprint_id, artifact_id)
    if storage.artifact_root(sprint_id, req.target_id) != root_id:
        raise HTTPException(status_code=400, detail="目标版本不在同一版本链上")
    version = storage.latest_artifact_version(sprint_id, root_id) + 1
    # 旧版带真实文件时必须复制为新文件，不能与旧版共享 file_name（删除会互相影响）
    file_name = None
    degraded = False
    degraded_reason = ""
    if target.get("file_name"):
        file_name = await asyncio.to_thread(
            forge.duplicate_deliverable, target["file_name"]
        )
        if not file_name:
            degraded = True
            degraded_reason = "旧版文件已丢失，仅恢复文字内容"
    collaborative_final = (artifact.get("kind") == "final" and
                           (storage.get_sprint(sprint_id) or {}).get("execution_style") == "collaborative")
    if collaborative_final and running:
        raise HTTPException(status_code=409, detail="当前协作 Sprint 仍在运行，结束后再回滚")
    rollback_regen = artifact.get("regen")
    if collaborative_final:
        import hashlib
        path = forge.safe_deliverable_path(file_name or "")
        file_kind = Path(file_name).suffix.lstrip(".").lower() if file_name else ""
        rollback_regen = {"mode": "manual_revision", "file_kind": "code" if file_kind == "zip" else file_kind,
                          "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else "",
                          "contribution_refs": _manual_contribution_refs(sprint_id, artifact)}
    created = add_artifact(
        kind=target.get("kind") or artifact.get("kind", "custom"),
        title=f"{artifact.get('title', '产物')}（回滚 v{version}）",
        author_roster=artifact.get("author_roster"),
        content=target.get("content", ""),
        task_id=artifact.get("task_id"),
        file_name=file_name,
        degraded=degraded,
        degraded_reason=degraded_reason,
        source=target.get("source", "unknown"),
        quality_status="failed" if degraded else "unchecked" if collaborative_final else target.get("quality_status", "unchecked"),
        regen=rollback_regen,
        parent_id=root_id,
        version=version,
        sprint_id=sprint_id,
    )
    if collaborative_final and not created.get("degraded"):
        created = await _revalidate_manual_collaborative_version(created, sprint_id)
    return {"ok": True, "artifact": created}


@app.get("/api/artifacts/{artifact_id}/raw")
async def artifact_raw(artifact_id: str):
    """返回产物的文字内容供站内编辑器加载，上限 200KB"""
    artifact = _find_artifact(artifact_id)
    content = artifact.get("content") or ""
    path = forge.safe_deliverable_path(artifact.get("file_name") or "")
    if path and Path(path).suffix.lower() in (".docx", ".pdf"):
        import artifact_quality
        source = artifact_quality.report_directory(path, forge.WORKSPACE_DIR) / "source.md"
        if source.is_file():
            content = source.read_text(encoding="utf-8")
    return {
        "id": artifact_id,
        "title": artifact.get("title", ""),
        "content": content[:200_000],
        "file_name": artifact.get("file_name") or "",
        "truncated": len(content) > 200_000,
    }


class EditRequest(BaseModel):
    content: str


@app.post("/api/artifacts/{artifact_id}/edit")
async def edit_artifact(artifact_id: str, req: EditRequest):
    """站内文本编辑：修改后的内容存为版本链上的新版本（不走 LLM，秒级保存）。

    原产物带 docx 真实文件时，用确定性渲染把 Markdown 重新生成 Word 文件；
    其余产物仅保存文字内容，原真实文件保持不动。"""
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="内容不能为空")
    if len(content) > 200_000:
        raise HTTPException(status_code=400, detail="内容超过 200KB，无法保存")
    artifact = _find_artifact(artifact_id)
    sprint_id = artifact.get("sprint_id") or _current_sprint_id()
    collaborative_final = (artifact.get("kind") == "final" and
                           (storage.get_sprint(sprint_id) or {}).get("execution_style") == "collaborative")
    if collaborative_final and running:
        raise HTTPException(status_code=409, detail="当前协作 Sprint 仍在运行，结束后再编辑")
    root_id = storage.artifact_root(sprint_id, artifact_id)
    version = storage.latest_artifact_version(sprint_id, root_id) + 1

    file_name = None
    degraded = False
    degraded_reason = ""
    old_file = (artifact.get("file_name") or "").lower()
    if old_file.endswith((".docx", ".pdf")):
        requirement = (storage.get_sprint(sprint_id) or {}).get("requirement", "")
        base = forge.semantic_stem(requirement or artifact.get("title", ""), "文档")
        file_name = await asyncio.to_thread(
            forge.render_document_from_markdown, content, f"{base}（编辑v{version}）{Path(old_file).suffix}"
        )
        if not file_name:
            degraded = True
            degraded_reason = "Word 文件渲染失败，本次仅保存了文字内容"
    import hashlib
    source_path = forge.safe_deliverable_path(file_name or "")
    file_kind = Path(file_name).suffix.lstrip(".").lower() if file_name else ""
    edit_regen = {"mode": "manual_revision", "file_kind": "code" if file_kind == "zip" else file_kind,
                  "sha256": hashlib.sha256(Path(source_path).read_bytes()).hexdigest() if source_path else "",
                  "contribution_refs": _manual_contribution_refs(sprint_id, artifact)}
    created = add_artifact(
        kind=artifact.get("kind", "custom"),
        title=f"{artifact.get('title', '产物')}（编辑 v{version}）",
        author_roster=artifact.get("author_roster"),
        content=content,
        task_id=artifact.get("task_id"),
        file_name=file_name,
        degraded=degraded,
        degraded_reason=degraded_reason,
        source="local",
        quality_status="failed" if degraded else "unchecked",
        regen=edit_regen,
        parent_id=artifact_id,
        version=version,
        sprint_id=sprint_id,
    )
    if collaborative_final and not created.get("degraded"):
        created = await _revalidate_manual_collaborative_version(created, sprint_id)
    return {"ok": True, "artifact": created}


@app.get("/api/artifacts/{artifact_id}/code_preview")
async def code_preview_artifact(artifact_id: str):
    """HTML 代码产物直接以 text/html 返回，供前端 iframe sandbox 隔离预览"""
    artifact = _find_artifact(artifact_id)
    file_name = artifact.get("file_name") or ""
    if not file_name.lower().endswith(".html"):
        raise HTTPException(status_code=400, detail="仅 HTML 产物支持沙箱预览")
    path = forge.safe_deliverable_path(file_name)
    if not path:
        raise HTTPException(status_code=404, detail="文件不存在或已被清理")
    return FileResponse(path, media_type="text/html")


@app.post("/api/artifacts/generate")
async def generate_artifact(req: GenerateRequest):
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    if not llm_available():
        raise HTTPException(
            status_code=400,
            detail="尚未配置任何成员的专属 API，无法生成：请先在「智能体 → ⚙ 设置」中为成员填入 API Key",
        )
    if not running or not _current_sprint_id():
        # 空闲时直接生成成品：单独建立一条可追溯记录，不混进上一轮 Sprint
        sprint_id = storage.create_sprint(prompt[:200] or "直接生成成品")
        sprint_meta.update(
            {
                "id": sprint_id,
                "requirement": prompt[:200],
                "started_at": time.time(),
                "finished_at": time.time(),
                "status": "running",
                "stage": "直接生成成品",
                "deliver_kind": _detect_file_kind(prompt) or "docx",
            }
        )
        artifact_store.set_sprint(sprint_id)
        tasks_store.clear()
        bus.event_log.clear()
        storage.update_sprint(
            sprint_id,
            status="running",
            stage="直接生成成品",
            deliver_kind=sprint_meta["deliver_kind"],
            finished_at=time.time(),
        )
        bus.emit({"type": "hello", **(await get_state())})
    asyncio.create_task(_generate_document_safe(prompt, req.upload_ids, _current_sprint_id()))
    return {"ok": True, "sprint_id": _current_sprint_id(), "message": "生成任务已提交，完成后会实时出现在产物中心"}


# ---------------------------------------------------------------------------
# 对话中心：以角色身份与老板聊天（成员专属 API 驱动，未配 Key 时 Mock 兜底）
# ---------------------------------------------------------------------------
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    roster: int
    message: str
    history: list[ChatMessage] = []
    upload_ids: list[str] = []  # 可附带图片附件，走视觉模型识别


@app.post("/api/chat")
async def chat(req: ChatRequest):
    entry = ROSTER.get(req.roster)
    if not entry:
        raise HTTPException(status_code=400, detail="roster 必须在 1-6")
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message 不能为空")

    system = _chat_system(req.roster)
    local_reply = clock_reply(text)
    if local_reply:
        return {"reply": local_reply, "llm": False, "model": "系统时钟", "roster": req.roster, "degraded": False}
    history = [
        {"role": m.role, "content": m.content}
        for m in req.history[-12:]
        if m.role in ("user", "assistant")
    ]
    history.append({"role": "user", "content": text})

    meta: dict = {}
    images = uploads.extract_images(req.upload_ids)
    reply = await asyncio.to_thread(
        chat_text,
        system,
        history,
        lambda: unavailable_reply(text),
        roster=req.roster,
        meta=meta,
        images=images or None,
    )
    return {
        "reply": reply,
        "llm": llm_available(req.roster),
        "model": llm.effective_model(req.roster),
        "roster": req.roster,
        "degraded": bool(meta.get("fallback_reason")),
        "backup_used": bool(meta.get("backup_used")),
        "images_attached": len(images),
        "images_dropped": bool(meta.get("images_dropped")),
    }


def _chat_system(roster: int) -> str:
    cfg = agent_config.get_agent_config(roster)
    entry = {**ROSTER[roster], "name": cfg.get("name") or ROSTER[roster]["name"]}
    return build_chat_system(entry, roster, sprint_meta, tasks_store, artifacts_store)


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """流式对话（SSE）：逐段推送模型输出，前端可边生成边显示、可中断"""
    if req.roster not in ROSTER:
        raise HTTPException(status_code=400, detail="roster 必须在 1-6")
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message 不能为空")
    history = [
        {"role": m.role, "content": m.content}
        for m in req.history[-12:]
        if m.role in ("user", "assistant")
    ]
    history.append({"role": "user", "content": text})
    fallback = unavailable_reply(text)
    meta: dict = {}
    system = _chat_system(req.roster)
    local_reply = clock_reply(text)
    # 图片附件：把最后一条 user 消息升级为多模态内容（模型不支持时整段回退兜底文案）
    images = uploads.extract_images(req.upload_ids)
    if images:
        history[-1] = {**history[-1], "content": llm._with_images(text, images)}

    async def event_source():
        import json as _json

        if local_reply:
            for payload in ({"start": True, "model": "系统时钟"}, {"delta": local_reply}, {"done": True, "degraded": False}):
                yield f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"
            return
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def worker() -> None:
            try:
                for chunk in stream_chat(system, history, req.roster, fallback, meta):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=worker, daemon=True).start()
        yield f"data: {_json.dumps({'model': llm.effective_model(req.roster) or '', 'start': True}, ensure_ascii=False)}\n\n"
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield f"data: {_json.dumps({'delta': chunk}, ensure_ascii=False)}\n\n"
        yield f"data: {_json.dumps({'done': True, 'degraded': bool(meta.get('fallback_reason')), 'images_dropped': bool(meta.get('images_dropped')), 'backup_used': bool(meta.get('backup_used'))}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# 成员配置：自定义姓名 + 每个成员独立的 LLM API
# ---------------------------------------------------------------------------
class AgentConfigPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # "" 或省略 = 保留旧值；"__CLEAR__" = 清除专属 key
    model: str | None = None
    avatar: str | None = None  # emoji 头像，最长 8 字符
    skin: str | None = None  # 办公室人物形象皮肤：空串=默认按名册分配
    backup_base_url: str | None = None  # 备用 API：主通道故障时自动切换
    backup_api_key: str | None = None
    backup_model: str | None = None


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 8}{key[-4:]}"


def _agent_view(roster: int) -> dict:
    entry = ROSTER.get(roster, {})
    cfg = agent_config.get_agent_config(roster)
    llm_cfg = cfg["llm"]
    backup_cfg = cfg["llm_backup"]
    has_key = bool(llm_cfg["api_key"])
    has_backup_key = bool(backup_cfg["api_key"])
    backup_in_use = llm.backup_in_use(roster)
    failure = llm.agent_failure(roster)
    availability = "unconfigured" if not has_key else "backup" if backup_in_use else "error" if failure else "configured"
    return {
        "roster": roster,
        "name": entry.get("name", f"#{roster}"),
        "role": entry.get("role", ""),
        "avatar": cfg.get("avatar", ""),
        "skin": cfg.get("skin", ""),
        # 永不回传真实 Key：只给掩码提示，修改时留空即保留旧值
        "llm": {
            "base_url": llm_cfg["base_url"],
            "api_key": "",
            "api_key_masked": _mask_key(llm_cfg["api_key"]),
            "model": llm_cfg["model"],
        },
        "llm_backup": {
            "base_url": backup_cfg["base_url"],
            "api_key": "",
            "api_key_masked": _mask_key(backup_cfg["api_key"]),
            "model": backup_cfg["model"],
        },
        "has_custom_key": has_key,
        "has_backup_key": has_backup_key,
        "backup_in_use": backup_in_use,
        "availability": availability,
        "availability_reason": failure["reason"] if failure else ("未配置主 API Key" if not has_key else ""),
        "availability_updated_at": failure["ts"] if failure else None,
    }


@app.get("/api/agents")
async def list_agents():
    return {"agents": [_agent_view(no) for no in sorted(ROSTER)]}


@app.get("/api/usage")
async def get_usage():
    """各成员 LLM 调用用量与估算成本（按公开刊例约值估算，仅供演示参考）"""
    report = llm.usage_report()
    for member in report["members"]:
        member["name"] = roster_name(member["roster"])
    baseline = sprint_meta.get("usage_baseline") or 0.0
    report["sprint_cost"] = max(0.0, round(llm.usage_total_cost() - baseline, 4))
    report["sprint_tokens"] = llm.usage_total_tokens()
    return report


@app.get("/api/usage/export.csv")
async def export_usage_csv():
    """用量明细导出为 CSV（带 BOM，Excel 直接打开不乱码）"""
    report = llm.usage_report()
    lines = ["成员编号,成员姓名,模型,调用次数,输入tokens,输出tokens,估算花费(元)"]
    for member in report["members"]:
        lines.append(
            f"{member['roster']},{roster_name(member['roster'])},{member['model']},"
            f"{member['calls']},{member['prompt_tokens']},{member['completion_tokens']},"
            f"{member['cost']}"
        )
    lines.append(f"合计,,,,,,{report['total_cost']}")
    content = "﻿" + "\n".join(lines)  # 前缀 BOM 让 Excel 正确识别 UTF-8
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="usage.csv"'},
    )


@app.get("/api/pricing")
async def get_pricing():
    """当前生效的模型单价表（元/百万 tokens，含自定义覆盖）"""
    return llm.pricing_view()


class PricingRequest(BaseModel):
    prices: dict


@app.put("/api/pricing")
async def put_pricing(req: PricingRequest):
    """自定义模型单价：{模型名: [输入价, 输出价]}；值为 null 时恢复内置价"""
    try:
        return llm.update_pricing(req.prices)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=400, detail=str(error))


# ---------------------------------------------------------------------------
# 数据备份：数据库/用量/配置/产物一键导出导入，换机或重装不再丢历史
# ---------------------------------------------------------------------------
BACKUP_DB_ITEMS = ("usage.json", "agent_config.json", "pricing.json", "skills_data.json")


@app.get("/api/backup/export")
async def backup_export():
    """导出全部数据：office.db + 用量/成员配置/单价 + deliverables 产物目录"""
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    zip_name = forge._unique_name("office-backup.zip")
    zip_path = forge.output_path(zip_name)

    def _pack() -> None:
        # 持锁拷贝数据库，避免导到一半的写入事务
        with storage._lock:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                if os.path.isfile(storage.DB_PATH):
                    zf.write(storage.DB_PATH, "office.db")
                for name in BACKUP_DB_ITEMS:
                    path = os.path.join(backend_dir, name)
                    if os.path.isfile(path):
                        zf.write(path, name)
                for base in (forge.DELIVERABLES_DIR, forge.WORKSPACE_DIR):
                    for root, _dirs, files in os.walk(base):
                        _dirs[:] = [name for name in _dirs if name not in (".venv", "node_modules", "__pycache__")]
                        if os.path.realpath(root) == os.path.realpath(forge.WORKSPACE_DIR):
                            _dirs[:] = [name for name in _dirs if name not in ("tools", "benchmarks")]
                        for filename in files:
                            full = os.path.join(root, filename)
                            if os.path.abspath(full) == os.path.abspath(zip_path) or filename == "office-backup.zip":
                                continue
                            zf.write(full, os.path.relpath(full, backend_dir))

    await asyncio.to_thread(_pack)
    return FileResponse(zip_path, filename="office-backup.zip", media_type="application/zip")


@app.post("/api/backup/import")
async def backup_import(file: UploadFile):
    """导入备份 zip：恢复数据库/配置/产物文件。Sprint 运行中拒绝导入"""
    if running:
        raise HTTPException(status_code=409, detail="Sprint 运行中，请先结束再导入备份")
    data = await file.read()
    if len(data) > 500 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="备份文件超过 500MB，无法导入")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="备份文件不是有效的 zip 包")

    backend_dir = os.path.dirname(os.path.abspath(__file__))
    restored: list[str] = []
    with archive:
        names = set(archive.namelist())

        def _restore() -> None:
            with storage._lock:
                if "office.db" in names:
                    with archive.open("office.db") as src, open(storage.DB_PATH, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    restored.append("office.db")
                for name in BACKUP_DB_ITEMS:
                    if name in names:
                        with archive.open(name) as src, open(
                            os.path.join(backend_dir, name), "wb"
                        ) as dst:
                            shutil.copyfileobj(src, dst)
                        restored.append(name)
                for info in archive.infolist():
                    if info.is_dir() or not info.filename.startswith(("deliverables/", "workspace/")):
                        continue
                    if info.filename.startswith(("workspace/tools/", "workspace/benchmarks/")):
                        continue
                    if any(part in (".venv", "node_modules", "__pycache__") for part in info.filename.replace("\\", "/").split("/")):
                        continue
                    rel = os.path.normpath(info.filename)
                    if ".." in info.filename.replace("\\", "/").split("/") or os.path.commonpath([os.path.realpath(backend_dir), os.path.realpath(os.path.join(backend_dir, rel))]) != os.path.realpath(backend_dir):
                        continue
                    target = os.path.join(backend_dir, rel)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with archive.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    restored.append(rel)

        await asyncio.to_thread(_restore)
    if not restored:
        raise HTTPException(status_code=400, detail="备份包内没有可恢复的数据文件")
    if "skills_data.json" in restored:
        for item in skills.list_skills():
            if item.get("package_id"):
                skills.update_package_status(item["id"], item.get("tools", []),
                                             ["备份恢复后请重新安装依赖"])
    return {
        "ok": True,
        "restored": restored,
        "note": "导入完成。请重启后端并刷新页面；完整技能包的依赖需在技能中心点击重试安装。",
    }


class AgentTestRequest(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


@app.post("/api/agents/{roster}/test")
async def test_agent(roster: int, req: AgentTestRequest):
    """用弹窗里当前填写的配置真实调一次模型，验证配置是否可用。
    api_key 留空时改用已保存的 key；base_url/model 留空走默认值。"""
    if roster not in ROSTER:
        raise HTTPException(status_code=404, detail="成员不存在")

    saved = agent_config.get_agent_config(roster)["llm"]
    api_key = (req.api_key or "").strip() or saved.get("api_key", "")
    base_url = ((req.base_url or "").strip() or saved.get("base_url") or llm.DEFAULT_BASE_URL).rstrip("/")
    model = (req.model or "").strip() or saved.get("model") or llm.DEFAULT_MODEL
    if not api_key:
        return {"ok": False, "error": "未配置 API Key"}
    start = time.time()
    try:
        reply = await asyncio.to_thread(llm.ping, base_url, api_key, model, 60)
        if api_key == saved.get("api_key", ""):
            llm._mark_success(roster, api_key)
        return {"ok": True, "reply": reply.strip(), "latency": round(time.time() - start, 2)}
    except Exception as error:  # noqa: BLE001 —— 把真实错误透传给前端展示
        if api_key == saved.get("api_key", ""):
            llm._mark_fallback(roster, str(error))
        return {"ok": False, "error": str(error)[:200]}


@app.put("/api/agents/{roster}")
async def update_agent(roster: int, patch: AgentConfigPatch):
    if roster not in ROSTER:
        raise HTTPException(status_code=400, detail="roster 必须在 1-6")
    # 防污染：前端遮罩显示用的占位值（含 … 或 *）不允许落库为真实 Key
    if patch.api_key and ("…" in patch.api_key or "*" in patch.api_key):
        raise HTTPException(
            status_code=400,
            detail="API Key 包含遮罩占位符，疑似把星号显示值当成真实 Key 保存，请重新输入完整 Key",
        )
    llm_patch = {
        k: v
        for k, v in {
            "base_url": patch.base_url,
            "api_key": patch.api_key,
            "model": patch.model,
        }.items()
        if v is not None
    }
    # 备用 API 同样拒绝遮罩占位值
    if patch.backup_api_key and ("…" in patch.backup_api_key or "*" in patch.backup_api_key):
        raise HTTPException(
            status_code=400,
            detail="备用 API Key 包含遮罩占位符，请重新输入完整 Key",
        )
    backup_patch = {
        k: v
        for k, v in {
            "base_url": patch.backup_base_url,
            "api_key": patch.backup_api_key,
            "model": patch.backup_model,
        }.items()
        if v is not None
    }
    try:
        agent_config.update_agent_config(
            roster,
            name=patch.name,
            llm=llm_patch or None,
            llm_backup=backup_patch or None,
            avatar=patch.avatar,
            skin=patch.skin,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _agent_view(roster)


# ---------------------------------------------------------------------------
# 文件中心：当前 Sprint 的需求、任务清单与自动生成的日报
# ---------------------------------------------------------------------------
def build_report_md(requirement: str, tasks: list[dict]) -> str:
    lines = [
        "# Sprint 日报",
        "",
        f"- 需求：{requirement or '（未记录）'}",
        f"- 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 状态：{'进行中' if running else '已完成/空闲'}",
        "",
        "## 任务清单",
    ]
    for task in tasks:
        mark = "x" if task.get("status") == "通过" else " "
        assignee = (
            roster_name(task["assignee_roster"])
            if task.get("assignee_roster")
            else "未分配"
        )
        lines.append(
            f"- [{mark}] {task.get('id', '')} {task.get('title', '')}"
            f"（{task.get('status', '')}，{assignee}）"
        )
    lines += ["", "## 交付说明", "", "由多 Agent 敏捷开发系统自动生成。", ""]
    return "\n".join(lines)


@app.get("/api/files")
async def files():
    if not running:
        # 空闲状态没有「当前 Sprint 产物」：上一轮内容请从历史 Sprint 详情回看
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
        "running": running,
        "updated_at": sprint_meta["finished_at"] or sprint_meta["started_at"],
        "tasks": tasks_store,
        "artifacts": artifacts_store,
        "report_md": build_report_md(sprint_meta["requirement"], tasks_store),
    }


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "running": running,
        "llm": llm_available(),
        "ws_clients": len(clients),
        "ts": time.time(),
    }


@app.websocket("/ws/office")
async def ws_office(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    print(f"[WS] 客户端接入，当前 {len(clients)} 个")
    try:
        # 完整快照：刷新/断线重连后可恢复任务、产物、阶段与运行状态
        # 已结束的 Sprint 不再作为「当前轮次」下发，避免新页面凭空出现上一轮的旧任务
        active = _active_round()
        await ws.send_json(
            {
                "type": "hello",
                "llm": llm_available(),
                "running": running,
                "paused": bus.is_paused(),
                "tasks": active["tasks"],
                "artifacts": active["artifacts"],
                "sprint": active["sprint"],
                "events": bus.event_log[-80:],
                "usage": llm.usage_report(),
            }
        )
        while True:
            # 仅保活；前端目前不需要向服务端发指令
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        print(f"[WS] 客户端断开，剩余 {len(clients)} 个")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
