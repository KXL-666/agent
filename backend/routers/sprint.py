"""Sprint 生命周期与任务管理路由。

- POST /api/sprint/plan       预览本轮拆解计划
- POST /api/sprint/start      发起一轮 Sprint
- POST /api/demo/start        零成本演示模式
- POST /api/sprint/stop       手动结束
- POST /api/sprint/pause      真暂停
- POST /api/sprint/resume     解除暂停
- POST /api/sprint/instruct   补充指示
- POST /api/tasks/{id}/cancel 取消单个任务
- POST /api/tasks/{id}/reassign 改派任务
"""
from __future__ import annotations

import asyncio
import os
import time
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import file_knowledge
import forge
import llm
import storage
import uploads
import artifacts as artifact_store
import events as bus
from events import sprint_meta, tasks_store
from graph import add_instruction
from llm import llm_available
from requirement_contract import build_contract
from roles import ROSTER
from route_helpers import (
    _detect_file_kind,
    _find_task,
    _local_model_ready,
)
import runtime_state
from sprint_runner import _run_demo, run_sprint

router = APIRouter(tags=["sprint"])


class SprintRequest(BaseModel):
    requirement: str
    upload_ids: list[str] = []
    fast: bool = False
    save_dir: str = ""
    budget: float = 0.0
    task_kind: str = "auto"
    execution_mode: str = "cloud"
    execution_style: str = "collaborative"


class InstructionRequest(BaseModel):
    text: str


class ReassignRequest(BaseModel):
    roster: int


class RecoverRequest(BaseModel):
    checkpoint_id: str
    request_id: str


@router.get("/api/privacy/capabilities")
async def privacy_capabilities():
    return {"local_model_ready": _local_model_ready(), "message": "本地模式要求所有已配置的主、备用模型 API 位于本机回环地址。"}


@router.post("/api/sprint/plan")
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


@router.post("/api/sprint/start")
async def start_sprint(req: SprintRequest):
    if runtime_state.requires_restart:
        raise HTTPException(409, "数据库已恢复，请重启后端后再开始任务")
    if storage.revision_operations(active=True):
        raise HTTPException(409, '产物修订仍在执行，请完成后再开始 Sprint')
    requirement = req.requirement.strip()
    if not requirement:
        raise HTTPException(status_code=400, detail="requirement 不能为空")
    if runtime_state.running:
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

    sprint_id = storage.create_sprint(requirement, budget=budget)
    storage.update_sprint(sprint_id, execution_style=req.execution_style,
                          execution_mode=req.execution_mode, budget=budget)
    save_dir = os.path.expandvars(os.path.expanduser(req.save_dir.strip()))
    if save_dir:
        try:
            os.makedirs(save_dir, exist_ok=True)
        except OSError as error:
            raise HTTPException(status_code=400, detail=f"保存位置不可用：{error}")
    forge.set_explicit_save_dir(save_dir)
    from sprint_recovery import SCHEMA, signature, owner
    from event_broadcaster import restore_instructions
    restore_instructions([])
    if runtime_state.persistent_ready and not direct_artifact:
        storage.update_sprint(sprint_id, run_options_json=json.dumps({
            "schema": SCHEMA, "workflow_signature": signature(),
            "fast": bool(req.fast), "save_dir": save_dir, "task_kind": req.task_kind,
            "owner": owner(),
        }, ensure_ascii=False))
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
    bus.event_log.clear()
    bus.set_paused(False)
    artifact_store.set_sprint(sprint_id)
    forge.begin_sprint(sprint_id)

    runtime_state.running = True
    uploads_text = file_knowledge.context_for_task(requirement, req.upload_ids)
    preliminary_contract = build_contract(
        requirement, uploads_text,
        deliver_kind=_detect_file_kind(requirement) if direct_artifact else "",
    )
    storage.save_contract(sprint_id, preliminary_contract)
    uploads_images = uploads.extract_images(req.upload_ids)
    runtime_state.sprint_task = asyncio.create_task(
        run_sprint(requirement, uploads_text, req.fast, uploads_images, direct_artifact, req.upload_ids, req.task_kind)
    )
    return {"ok": True, "sprint_id": sprint_id, "requirement": requirement, "uploads": len(req.upload_ids)}


@router.get("/api/sprint/recoverable")
async def recoverable_sprints():
    from sprint_recovery import plan
    rows = [row for row in storage.list_sprints(100) if row["status"] in ("interrupted", "failed")][:20]
    return {"sprints": [await plan(row["id"]) for row in rows]}


@router.get("/api/sprints/{sprint_id}/recovery")
async def sprint_recovery_plan(sprint_id: str):
    from sprint_recovery import plan
    return await plan(sprint_id)


@router.post("/api/sprints/{sprint_id}/recover", status_code=202)
async def recover_sprint(sprint_id: str, req: RecoverRequest):
    from sprint_recovery import plan, owner, validate_files, activate
    if not req.request_id.strip() or len(req.request_id) > 100:
        raise HTTPException(400, "续跑请求标识无效")
    async with runtime_state.recovery_lock:
        if runtime_state.requires_restart:
            raise HTTPException(409, "数据库已恢复，请重启后端后再续跑")
        sprint = storage.get_sprint(sprint_id)
        if not sprint:
            raise HTTPException(404, "Sprint 不存在")
        options = sprint.get("run_options") or {}
        previous = storage.get_resume_request(sprint_id, req.request_id)
        if previous:
            if previous.get("checkpoint_id") != req.checkpoint_id:
                raise HTTPException(409, "同一请求标识不能用于不同检查点")
            return {"ok": True, "sprint_id": sprint_id, "reused": True, "status": sprint["status"]}
        if runtime_state.running or storage.revision_operations(active=True):
            raise HTTPException(409, "已有任务正在执行，请先结束后再续跑")
        preview = await plan(sprint_id)
        if not preview["resumable"]:
            raise HTTPException(409, preview["reason"])
        if preview["checkpoint_id"] != req.checkpoint_id:
            raise HTTPException(409, "检查点已变化，请重新查看续跑计划")
        if preview["next_nodes"] and not llm_available():
            raise HTTPException(400, "请先配置可用的成员模型 API")
        if preview["next_nodes"] and sprint.get("execution_mode") == "local" and not _local_model_ready():
            raise HTTPException(400, "本地模式要求主、备用模型均位于本机回环地址")
        snapshot = await runtime_state.graph.aget_state({"configurable": {"thread_id": sprint_id}})
        if snapshot.config["configurable"]["checkpoint_id"] != req.checkpoint_id:
            raise HTTPException(409, "检查点已变化，请重新查看续跑计划")
        problems = validate_files(snapshot.values)
        if problems:
            raise HTTPException(409, "；".join(problems[:5]))
        # Recheck after all awaited reads; a new Sprint may have started meanwhile.
        if runtime_state.running or storage.revision_operations(active=True):
            raise HTTPException(409, "已有任务正在执行")
        save_dir = str(options.get("save_dir") or "")
        if save_dir:
            try:
                os.makedirs(save_dir, exist_ok=True)
            except OSError:
                raise HTTPException(400, "原保存位置不可用，请恢复该目录权限后重试")
        options["last_resume_request"] = {"id": req.request_id, "checkpoint_id": req.checkpoint_id}
        options["owner"] = owner()
        if not storage.claim_sprint_resume(sprint_id, options):
            raise HTTPException(409, "任务状态已变化，请刷新后重试")
        runtime_state.running = True
        try:
            activate(sprint, snapshot.values, options)
            runtime_state.sprint_task = asyncio.create_task(run_sprint(
                sprint["requirement"], fast=bool(options.get("fast")),
                task_kind=options.get("task_kind", "auto"), resume=True))
        except Exception:
            runtime_state.running = False
            sprint_meta.update(status="interrupted", stage="续跑准备失败，未启动模型调用")
            storage.update_sprint(sprint_id, status="interrupted", stage=sprint_meta["stage"], finished_at=time.time())
            forge.set_explicit_save_dir("")
            raise HTTPException(503, "续跑准备失败，未启动模型调用；请检查文件权限并重启后端后重试") from None
        return {"ok": True, "sprint_id": sprint_id, "reused": False, "next_steps": preview["next_steps"]}


@router.post("/api/demo/start")
async def start_demo():
    """零成本演示模式：脚本化回放一场完整 Sprint，不消耗任何 API 额度。"""
    if runtime_state.running or storage.revision_operations(active=True):
        raise HTTPException(status_code=409, detail="Sprint 正在进行中")
    runtime_state.running = True
    runtime_state.sprint_task = asyncio.create_task(_run_demo())
    return {"ok": True}


@router.post("/api/sprint/stop")
async def stop_sprint():
    """手动结束当前 Sprint：取消工作流，全员转空闲，并清掉本轮驻留状态。"""
    if not runtime_state.running or not runtime_state.sprint_task or runtime_state.sprint_task.done():
        raise HTTPException(status_code=409, detail="当前没有进行中的任务")
    bus.set_paused(False)
    runtime_state.sprint_task.cancel()
    bus.emit({"type": "sprint_stopped", "message": "任务已手动结束"})
    try:
        await runtime_state.sprint_task
    except BaseException:  # CancelledError 及工作流收尾异常都不影响清理
        pass
    # 手动结束的轮次不再驻留内存：对话与状态快照恢复干净，历史仍可在 storage 中查询
    tasks_store.clear()
    sprint_meta.update(id="", requirement="", status="idle", stage="", finished_at=time.time())
    return {"ok": True}


@router.post("/api/sprint/pause")
async def pause_sprint():
    """真暂停：后端工作流在下一个检查点阻塞，不再产生任何 LLM 调用。"""
    if not runtime_state.running:
        raise HTTPException(status_code=409, detail="当前没有进行中的任务")
    bus.set_paused(True)
    bus.emit({"type": "sprint_paused", "message": "已暂停：模型调用已全部挂起"})
    return {"ok": True, "paused": True}


@router.post("/api/sprint/resume")
async def resume_sprint():
    """解除暂停闸门，工作流从挂起点继续。"""
    bus.set_paused(False)
    bus.emit({"type": "sprint_resumed", "message": "已恢复：团队继续工作"})
    return {"ok": True, "paused": False}


@router.post("/api/sprint/instruct")
async def instruct_sprint(req: InstructionRequest):
    """暂停/运行中补充老板指示：下一处 LLM 调用前自动注入提示词。"""
    if not runtime_state.running:
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


@router.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消单个任务：不再为其消耗模型配额，看板标记为「已取消」。"""
    task = _find_task(task_id)
    if task.get("status") in ("通过", "已取消"):
        raise HTTPException(
            status_code=400, detail=f"任务当前状态为「{task.get('status')}」，不能取消"
        )
    task["status"] = "已取消"
    storage.add_event(sprint_meta.get("id", ""), {"type": "task_control", "ts": time.time(),
                      "action": "cancel", "task_id": task_id})
    bus.emit({"type": "task_update", "task": task})
    return {"ok": True, "task": task}


@router.post("/api/tasks/{task_id}/reassign")
async def reassign_task(task_id: str, req: ReassignRequest):
    """把任务改派给另一位成员，看板与开发环节同步生效。"""
    task = _find_task(task_id)
    if req.roster not in ROSTER:
        raise HTTPException(status_code=400, detail="成员不存在")
    if task.get("status") in ("通过", "已取消"):
        raise HTTPException(
            status_code=400, detail=f"任务当前状态为「{task.get('status')}」，不能改派"
        )
    task["assignee_roster"] = req.roster
    storage.add_event(sprint_meta.get("id", ""), {"type": "task_control", "ts": time.time(),
                      "action": "reassign", "task_id": task_id, "roster": req.roster})
    bus.emit({"type": "task_update", "task": task})
    return {"ok": True, "task": task}
