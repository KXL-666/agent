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
import os
import shutil
import threading
import time
import zipfile
from contextlib import asynccontextmanager

import psutil
from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

import agent_config
import artifacts as artifact_store
import demo
import events as bus
import forge
import llm
import storage
import uploads
from artifacts import add_artifact, artifacts_store
from events import clients, sprint_meta, tasks_store
from graph import BudgetExceeded, add_instruction, build_sprint_graph
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
    yield
    if sprint_task and not sprint_task.done():
        sprint_task.cancel()


app = FastAPI(title="AI 敏捷开发办公室", lifespan=lifespan)

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


async def run_sprint(
    requirement: str,
    uploads_text: str = "",
    fast: bool = False,
    uploads_images: list | None = None,
) -> None:
    global running, sprint_task
    sprint_id = sprint_meta["id"]

    async def cost_ticker() -> None:
        """运行期间每 8 秒推送一次增量花费，让用户随时看到成本"""
        try:
            while True:
                await asyncio.sleep(8)
                bus.emit(
                    {
                        "type": "sprint_usage",
                        "cost": round(
                            llm.usage_total_cost() - sprint_meta["usage_baseline"], 4
                        ),
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
        result = await graph.ainvoke(
            {
                "requirement": requirement,
                "tasks": [],
                "rework_count": 0,
                "review_note": "",
                "deliver_kind": "",
                "uploads_text": uploads_text,
                "uploads_images": uploads_images or [],
            }
        )
        deliver_kind = result.get("deliver_kind", "") if isinstance(result, dict) else ""
        sprint_meta["deliver_kind"] = deliver_kind or sprint_meta.get("deliver_kind", "")
        sprint_meta["status"] = "completed"
        sprint_meta["stage"] = "已完成"
        storage.update_sprint(
            sprint_id,
            status="completed",
            stage="已完成",
            deliver_kind=sprint_meta["deliver_kind"],
            finished_at=time.time(),
        )
        bus.emit(
            {
                "type": "stage",
                "stage": "已完成",
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
    budget = max(0.0, float(req.budget or 0))
    if budget and budget < 0.01:
        raise HTTPException(status_code=400, detail="预算上限至少 0.01 元，或留 0 表示不限")
    if not llm_available():
        raise HTTPException(
            status_code=400,
            detail="尚未配置任何成员的专属 API，AI 无法开工：请先在「智能体 → ⚙ 设置」中为成员填入 API Key",
        )

    # 每轮 Sprint 独立入库，历史产物与代码不再被覆盖
    sprint_id = storage.create_sprint(requirement)
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
            "stage": "准备开工",
            "stage_started": time.time(),
            "deliver_kind": "",
            "usage_baseline": llm.usage_total_cost(),
            "usage_baseline_calls": llm.usage_total_calls(),
            "usage_baseline_tokens": llm.usage_total_tokens(),
            "fast": bool(req.fast),
            "budget": budget,
        }
    )
    tasks_store.clear()
    bus.event_log.clear()  # 新 Sprint 开启新的事件时间线（历史已落库可查）
    bus.set_paused(False)  # 新任务自动解除暂停闸门，避免一开始就被挂起
    artifact_store.set_sprint(sprint_id)
    forge.begin_sprint(sprint_id)

    running = True
    uploads_text = uploads.extract_many(req.upload_ids)
    uploads_images = uploads.extract_images(req.upload_ids)
    sprint_task = asyncio.create_task(
        run_sprint(requirement, uploads_text, req.fast, uploads_images)
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
        (saved if item else skipped).append(item or (f.filename or "unnamed"))
    return {"uploads": saved, "skipped_too_large": skipped}


@app.get("/api/uploads")
async def get_uploads():
    return {"uploads": uploads.list_uploads()}


@app.delete("/api/uploads/{upload_id}")
async def remove_upload(upload_id: str):
    if not uploads.delete_upload(upload_id):
        raise HTTPException(status_code=404, detail="附件不存在")
    return {"ok": True}


def _sprint_view(sprint_id: str | None) -> dict | None:
    if not sprint_id:
        return None
    return storage.get_sprint(sprint_id)


@app.get("/api/state")
async def get_state():
    sprint_id = sprint_meta.get("id")
    baseline = sprint_meta.get("usage_baseline") or 0.0
    return {
        "running": running,
        "paused": bus.is_paused(),
        "llm": llm_available(),
        "tasks": tasks_store,
        "artifacts": artifacts_store,
        "sprint": sprint_meta,
        "cost": round(llm.usage_total_cost() - baseline, 4) if running else 0,
        "history": storage.list_sprints(20),
    }


@app.get("/api/sprints")
async def list_sprints():
    return {"sprints": storage.list_sprints(50)}


@app.get("/api/sprints/{sprint_id}")
async def get_sprint_detail(sprint_id: str):
    sprint = _sprint_view(sprint_id)
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint 不存在")
    return {
        "sprint": sprint,
        "tasks": storage.list_tasks(sprint_id),
        "artifacts": storage.list_artifacts(sprint_id),
        "events": storage.list_events(sprint_id, 200),
    }


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
    zip_name = await asyncio.to_thread(_zip_files, files, f"sprint-{sprint_id}-产物.zip")
    return FileResponse(
        os.path.join(forge.DELIVERABLES_DIR, zip_name),
        filename=f"sprint-{sprint_id}-artifacts.zip",
    )


def _zip_files(paths: list[str], zip_name_hint: str) -> str:
    """把若干真实文件打成 zip（文件名维度去重），返回 zip 文件名"""
    zip_name = forge._unique_name(zip_name_hint)
    zip_path = os.path.join(forge.DELIVERABLES_DIR, zip_name)
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
            "当前没有成员配置专属 API，这是模板兜底文档。"
            "在「智能体 → ⚙ 设置」中为成员填入 API Key 后，这里将生成真实详实的交付物内容。\n\n"
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


_KIND_DEFAULT_NAME = {"docx": "文档.docx", "pptx": "演示文稿.pptx", "xlsx": "表格.xlsx"}


def _saved_note(dest: str | None, target_dir: str | None = None) -> str:
    if dest:
        return f"\n\n📁 已按你的要求保存到：{dest}"
    if target_dir:
        return f"\n\n⚠ 保存到「{target_dir}」失败：目录不可写或不存在，成品仍可在产物中心下载"
    return ""


def _current_sprint_id() -> str:
    return sprint_meta.get("id") or ""


async def _generate_document_safe(prompt: str, upload_ids: list[str] | None = None) -> None:
    """兜住生成过程中的任何异常：失败也落一条带警示的产物，不让用户空等"""
    try:
        await _generate_document(prompt, upload_ids)
    except Exception as error:  # noqa: BLE001
        print(f"[Generate] 生成失败：{error}")
        add_artifact(
            kind="custom",
            title="生成失败",
            author_roster=None,
            content=(
                f"# 生成失败\n\n需求：{prompt}\n\n错误：{error}\n\n"
                "可点击「重新生成」重试，或检查成员 API 配置后重试。"
            ),
            degraded=True,
            degraded_reason=str(error)[:160],
            regen={"mode": "custom", "prompt": prompt, "upload_ids": upload_ids or []},
        )


async def _generate_document(prompt: str, upload_ids: list[str] | None = None) -> None:
    """后台生成真实交付物文件并入库（自动经 WebSocket 推送 artifact_new）"""
    # 极简需求直通：「生成空文件」类指令无需 LLM，直接落盘并复制到目标位置
    if "空文件" in prompt:
        file_name = forge._unique_name("空文件.txt")
        with open(os.path.join(forge.DELIVERABLES_DIR, file_name), "w", encoding="utf-8") as f:
            f.write("")
        target_dir = forge.extract_target_dir(prompt)
        dest = (
            await asyncio.to_thread(forge.copy_to_target, file_name, target_dir)
            if target_dir
            else None
        )
        add_artifact(
            kind="custom",
            title="空文件.txt",
            author_roster=None,
            content=f"已生成空文件：{file_name}" + _saved_note(dest, target_dir),
            file_name=file_name,
            regen={"mode": "custom", "prompt": prompt, "upload_ids": upload_ids or []},
        )
        return

    kind = _detect_file_kind(prompt)
    attachments = uploads.extract_many(upload_ids or [])
    spec = (
        f"以下附件内容作为素材，请基于它们加工改造：\n\n{attachments}\n\n需求：{prompt}"
        if attachments else prompt
    )

    if kind:
        title = prompt[:18] or _KIND_DEFAULT_NAME[kind]
        bus.emit({"type": "generate_progress", "stage": "正在编写生成脚本", "prompt": prompt[:40]})
        file_name = await asyncio.to_thread(
            forge.forge_file, kind, spec, _KIND_DEFAULT_NAME[kind]
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
            check_failed = not check.get("ok", False)
            add_artifact(
                kind="custom",
                title=f"{title}（{_KIND_DEFAULT_NAME[kind].rpartition('.')[2]}）",
                author_roster=None,
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
                    "filename": _KIND_DEFAULT_NAME[kind],
                    "roster": None,
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
        title=_extract_title(content, prompt),
        author_roster=None,
        content=content,
        degraded=bool(reason),
        degraded_reason=reason,
        regen={
            "mode": "forge" if kind else "custom",
            "file_kind": kind,
            "spec": spec,
            "prompt": prompt,
            "filename": _KIND_DEFAULT_NAME.get(kind or "", ""),
            "upload_ids": upload_ids or [],
            "roster": None,
        },
    )


def _zip_code_for(sprint_id: str) -> str | None:
    """把指定 Sprint 的代码目录打成 zip，返回文件名；无代码时返回 None"""
    code_dir = os.path.realpath(forge.sprint_code_dir(sprint_id))
    if not os.path.isdir(code_dir):
        return None
    files = [os.path.join(root, f) for root, _, fs in os.walk(code_dir) for f in fs]
    if not files:
        return None
    zip_name = f"code-{sprint_id}.zip"
    zip_path = os.path.join(forge.DELIVERABLES_DIR, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for full in files:
            zf.write(full, os.path.relpath(full, code_dir))
    return zip_name


@app.get("/api/artifacts/code.zip")
async def download_code_zip(sprint: str | None = None):
    """打包下载指定 Sprint（默认当前）产出的全部代码"""
    sprint_id = sprint or _current_sprint_id()
    zip_name = await asyncio.to_thread(_zip_code_for, sprint_id)
    if not zip_name:
        raise HTTPException(status_code=404, detail="该 Sprint 尚未产出代码")
    return FileResponse(
        os.path.join(forge.DELIVERABLES_DIR, zip_name), filename="sprint-code.zip"
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
            "content": f"# 最终交付物\n\n- 代码包已重新打包：{zip_name}" + saved,
            "file_name": zip_name,
            "degraded": False,
            "degraded_reason": "",
        }

    if mode == "forge" and regen.get("file_kind"):
        spec = f"{regen.get('spec', '')}{suffix}"
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
        return {
            "content": (preview or f"已重新生成真实文件：{file_name}") + saved,
            "file_name": file_name,
            "degraded": False,
            "degraded_reason": "",
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
    }


@app.post("/api/artifacts/{artifact_id}/retry")
async def retry_artifact(artifact_id: str):
    """重新生成单个产物（保留同一条记录并刷新内容）"""
    artifact = _find_artifact(artifact_id)
    fields = await _rebuild(artifact)
    updated = artifact_store.update_artifact(artifact_id, **fields)
    return {"ok": True, "artifact": updated}


@app.post("/api/artifacts/{artifact_id}/revise")
async def revise_artifact(artifact_id: str, req: RevisionRequest):
    """基于现有产物继续修改：生成一个新版本，旧版本保留可追溯"""
    instruction = req.instruction.strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="修改要求不能为空")
    artifact = _find_artifact(artifact_id)
    fields = await _rebuild(artifact, instruction)
    sprint_id = artifact.get("sprint_id") or _current_sprint_id()
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
        regen=artifact.get("regen"),
        parent_id=artifact_id,
        version=version,
        sprint_id=sprint_id,
    )
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
    created = add_artifact(
        kind=target.get("kind") or artifact.get("kind", "custom"),
        title=f"{artifact.get('title', '产物')}（回滚 v{version}）",
        author_roster=artifact.get("author_roster"),
        content=target.get("content", ""),
        task_id=artifact.get("task_id"),
        file_name=file_name,
        degraded=degraded,
        degraded_reason=degraded_reason,
        regen=artifact.get("regen"),
        parent_id=root_id,
        version=version,
        sprint_id=sprint_id,
    )
    return {"ok": True, "artifact": created}


@app.get("/api/artifacts/{artifact_id}/raw")
async def artifact_raw(artifact_id: str):
    """返回产物的文字内容供站内编辑器加载，上限 200KB"""
    artifact = _find_artifact(artifact_id)
    content = artifact.get("content") or ""
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
    root_id = storage.artifact_root(sprint_id, artifact_id)
    version = storage.latest_artifact_version(sprint_id, root_id) + 1

    file_name = None
    degraded = False
    degraded_reason = ""
    old_file = (artifact.get("file_name") or "").lower()
    if old_file.endswith(".docx"):
        base = os.path.splitext(os.path.basename(artifact["file_name"]))[0]
        file_name = await asyncio.to_thread(
            forge.render_docx_from_markdown, content, f"{base}-编辑v{version}.docx"
        )
        if not file_name:
            degraded = True
            degraded_reason = "Word 文件渲染失败，本次仅保存了文字内容"
    created = add_artifact(
        kind=artifact.get("kind", "custom"),
        title=f"{artifact.get('title', '产物')}（编辑 v{version}）",
        author_roster=artifact.get("author_roster"),
        content=content,
        task_id=artifact.get("task_id"),
        file_name=file_name,
        degraded=degraded,
        degraded_reason=degraded_reason,
        regen=artifact.get("regen"),
        parent_id=artifact_id,
        version=version,
        sprint_id=sprint_id,
    )
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
                "status": "completed",
                "stage": "直接生成成品",
                "deliver_kind": _detect_file_kind(prompt) or "docx",
            }
        )
        artifact_store.set_sprint(sprint_id)
        storage.update_sprint(
            sprint_id,
            status="completed",
            stage="直接生成成品",
            deliver_kind=sprint_meta["deliver_kind"],
            finished_at=time.time(),
        )
    asyncio.create_task(_generate_document_safe(prompt, req.upload_ids))
    return {"ok": True, "message": "生成任务已提交，完成后会实时出现在产物中心"}


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


CHAT_FALLBACK: dict[int, list[str]] = {
    1: ["我先记到需求池，稍后拆解成任务。", "这个想法不错，我评估下优先级再排期。"],
    2: ["接口设计我来看下，稍后给你方案。", "这段逻辑后端来实现，问题不大。"],
    3: ["页面交互我来处理，先出个原型。", "样式细节我会盯住的，放心。"],
    4: ["我先补几条用例，把这个场景覆盖上。", "测出来问题我会第一时间打回。"],
    5: ["技术上可行，注意别引入过度设计。", "代码评审我来把关。"],
    6: ["流程没问题，保持当前节奏就好。", "我盯着看板，有阻塞随时喊我。"],
}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    entry = ROSTER.get(req.roster)
    if not entry:
        raise HTTPException(status_code=400, detail="roster 必须在 1-6")
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message 不能为空")

    system = (
        f"你是「AI 敏捷开发办公室」网页系统里的员工「{entry['role']}·{entry['name']}」（工位编号 {req.roster}）。"
        "团队正在通过 Sprint 协作交付软件：产品经理拆解需求，前后端工程师开发，测试工程师把关，"
        "技术主管评审代码，敏捷教练保障流程。请以该角色的身份和口吻回答老板的提问，"
        "口语化、简洁、自然，不超过 80 字。\n"
        "严格遵守以下事实边界，违反即为错误：\n"
        "1. 你和老板只在这个网页聊天窗口里对话。系统里不存在企业微信、微信、邮件、电话等任何外部渠道，"
        "绝不能说「已发你微信/邮箱/企业微信」或承诺通过任何外部渠道发送东西。\n"
        "2. 不要承诺发链接或发文件。老板要找需求、任务、产物、报告时，直接告诉他在本页面哪个模块查看："
        "任务进展在「任务中心」，需求与日报在「文件中心」，团队动态在「通知中心」，办公室实时画面在「首页」。\n"
        "3. 只谈论这个系统和当前 Sprint 里真实存在的事物，不确定的细节就坦率说稍后确认，不要编造。"
    )
    history = [
        {"role": m.role, "content": m.content}
        for m in req.history[-12:]
        if m.role in ("user", "assistant")
    ]
    history.append({"role": "user", "content": text})

    pool = CHAT_FALLBACK.get(req.roster, ["收到，我来跟进。"])
    meta: dict = {}
    images = uploads.extract_images(req.upload_ids)
    reply = await asyncio.to_thread(
        chat_text,
        system,
        history,
        lambda: pool[len(history) % len(pool)],
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
    entry = ROSTER[roster]
    return (
        f"你是「AI 敏捷开发办公室」网页系统里的员工「{entry['role']}·{entry['name']}」（工位编号 {roster}）。"
        "团队正在通过 Sprint 协作交付软件：产品经理拆解需求，前后端工程师开发，测试工程师把关，"
        "技术主管评审代码，敏捷教练保障流程。请以该角色的身份和口吻回答老板的提问，"
        "口语化、简洁、自然，不超过 80 字。\n"
        "严格遵守以下事实边界，违反即为错误：\n"
        "1. 你和老板只在这个网页聊天窗口里对话。系统里不存在企业微信、微信、邮件、电话等任何外部渠道，"
        "绝不能说「已发你微信/邮箱/企业微信」或承诺通过任何外部渠道发送东西。\n"
        "2. 不要承诺发链接或发文件。老板要找需求、任务、产物、报告时，直接告诉他在本页面哪个模块查看："
        "任务进展在「任务中心」，需求与日报在「文件中心」，团队动态在「通知中心」，办公室实时画面在「首页」。\n"
        "3. 只谈论这个系统和当前 Sprint 里真实存在的事物，不确定的细节就坦率说稍后确认，不要编造。"
    )


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
    pool = CHAT_FALLBACK.get(req.roster, ["收到，我来跟进。"])
    fallback = pool[len(history) % len(pool)]
    meta: dict = {}
    system = _chat_system(req.roster)
    # 图片附件：把最后一条 user 消息升级为多模态内容（模型不支持时整段回退兜底文案）
    images = uploads.extract_images(req.upload_ids)
    if images:
        history[-1] = {**history[-1], "content": llm._with_images(text, images)}

    async def event_source():
        import json as _json

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
        yield f"data: {_json.dumps({'done': True, 'degraded': bool(meta.get('fallback_reason'))}, ensure_ascii=False)}\n\n"

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
        "has_backup_key": bool(backup_cfg["api_key"]),
        "backup_in_use": llm.backup_in_use(roster),
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
    report["sprint_cost"] = round(llm.usage_total_cost() - baseline, 4)
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
BACKUP_DB_ITEMS = ("usage.json", "agent_config.json", "pricing.json")


@app.get("/api/backup/export")
async def backup_export():
    """导出全部数据：office.db + 用量/成员配置/单价 + deliverables 产物目录"""
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    zip_name = forge._unique_name("office-backup.zip")
    zip_path = os.path.join(forge.DELIVERABLES_DIR, zip_name)

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
                for root, _dirs, files in os.walk(forge.DELIVERABLES_DIR):
                    for filename in files:
                        full = os.path.join(root, filename)
                        if os.path.abspath(full) == os.path.abspath(zip_path):
                            continue
                        zf.write(full, os.path.relpath(full, backend_dir))

    await asyncio.to_thread(_pack)
    return FileResponse(zip_path, filename=zip_name, media_type="application/zip")


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
                    if info.is_dir() or not info.filename.startswith("deliverables/"):
                        continue
                    rel = os.path.normpath(info.filename)
                    if ".." in rel.split(os.sep):
                        continue
                    target = os.path.join(backend_dir, rel)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with archive.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    restored.append(rel)

        await asyncio.to_thread(_restore)
    if not restored:
        raise HTTPException(status_code=400, detail="备份包内没有可恢复的数据文件")
    return {
        "ok": True,
        "restored": restored,
        "note": "导入完成。建议重启后端并刷新页面，以加载全新的历史数据",
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
        return {"ok": True, "reply": reply.strip(), "latency": round(time.time() - start, 2)}
    except Exception as error:  # noqa: BLE001 —— 把真实错误透传给前端展示
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
        await ws.send_json(
            {
                "type": "hello",
                "llm": llm_available(),
                "running": running,
                "paused": bus.is_paused(),
                "tasks": tasks_store,
                "artifacts": artifacts_store,
                "sprint": sprint_meta,
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
