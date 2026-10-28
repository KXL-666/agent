"""Sprint 执行与产物生成核心逻辑。

拆分 main.py 时提取的异步执行函数：run_sprint（工作流驱动）、
demo 回放、直生成品（_generate_document 系列）、产物重试/修订（_rebuild）、
手动修订的预算守卫与 QA/LEAD 复跑、代码打包等。

这些函数被 sprint_router 与 artifacts_router 共同调用。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
import zipfile
from pathlib import Path

import forge
import llm
import storage
import graph as workflow_graph
from artifacts import add_artifact, artifacts_store
import events as bus
from events import sprint_meta, tasks_store
from graph import BudgetExceeded, add_instruction
from llm import chat_json, chat_text
from route_helpers import (
    _current_sprint_id,
    _detect_file_kind,
    _saved_note,
    _select_artifact_roster,
)
import runtime_state

_KIND_DEFAULT_NAME = {"docx": "文档.docx", "pptx": "演示文稿.pptx", "xlsx": "表格.xlsx", "pdf": "文档.pdf", "code": "项目.zip", "html": "页面.html"}


def _fallback_document(prompt: str) -> dict:
    """未配置 LLM 时的兜底文档，保证离线也能演示完整流程。"""
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
    """从 Markdown 首个一级标题提取文档标题，取不到则用需求前 18 字。"""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and len(stripped) > 2:
            return stripped[2:].strip()[:30]
    return prompt[:18] or "AI 生成文档"


def _zip_files(paths: list[str], zip_name_hint: str) -> str:
    """把若干真实文件打成 zip（文件名维度去重），返回 zip 文件名。"""
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


def _zip_code_for(sprint_id: str, requirement: str = "") -> str | None:
    """把指定 Sprint 的代码目录打成 zip，返回文件名；无代码时返回 None。"""
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


def _regen_spec(artifact: dict) -> dict:
    regen = artifact.get("regen") or {}
    if not regen:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="该产物没有保存生成参数，无法重试")
    return regen


def _manual_contribution_refs(sprint_id: str, artifact: dict) -> list[str]:
    regen = artifact.get("regen") or {}
    if regen.get("contribution_refs"):
        return list(regen["contribution_refs"])
    candidate = storage.get_artifact(regen.get("candidate_id") or "") or {}
    return list((candidate.get("regen") or {}).get("contribution_refs", []))


def _manual_call_guard(sprint_id: str):
    from route_helpers import _local_model_ready
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
    """手动协作修订版本走同样的 QA→LEAD 哈希门禁。"""
    import handoffs
    import review_decision
    import verification

    sprint = storage.get_sprint(sprint_id) or {}
    path = forge.safe_deliverable_path(artifact.get("file_name") or "")
    regen = artifact.get("regen") or {}
    if not path:
        from artifacts import update_artifact
        return update_artifact(
            artifact["id"], quality_status="unchecked", degraded_reason="修订版本缺少真实文件，QA/LEAD 未运行"
        )
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest != regen.get("sha256"):
        from artifacts import update_artifact
        return update_artifact(
            artifact["id"], quality_status="failed", degraded_reason="修订文件哈希与版本记录不一致"
        )
    if sprint.get("budget") and float(sprint.get("usage_cost") or 0) >= float(sprint.get("budget") or 0):
        from artifacts import update_artifact
        return update_artifact(
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
        from artifacts import update_artifact
        return update_artifact(artifact["id"], quality_status=status, degraded_reason=reason)
    finally:
        llm.reset_call_guard(token)
        _persist_manual_usage_delta(sprint_id, before_usage)


async def _rebuild(artifact: dict, instruction: str = "") -> dict:
    """按产物的 regen 参数重新生成内容/文件，返回可写回字段。"""
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
            from fastapi import HTTPException
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
            from fastapi import HTTPException
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
            "degraded": check.get("status", "passed" if check.get("ok") else "failed") == "failed",
            "degraded_reason": "重新生成的成品仍未通过质检" if check.get("status", "passed" if check.get("ok") else "failed") == "failed" else "",
            "source": "model",
            "quality_status": check.get("status", "passed" if check.get("ok") else "failed"),
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
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail="重新生成失败：模型未返回内容")
    return {
        "content": content,
        "degraded": bool(meta.get("fallback_reason")),
        "degraded_reason": meta.get("fallback_reason", ""),
        "source": "fallback" if meta.get("fallback_reason") else "model",
        "quality_status": "unchecked",
    }


async def _generate_document_safe(prompt: str, upload_ids: list[str] | None = None, sprint_id: str | None = None) -> None:
    """兜住生成过程中的任何异常：失败也落一条带警示的产物，不让用户空等。"""
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
            if sprint_meta.get("id") == sid and not runtime_state.running:
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
    """后台生成真实交付物文件并入库（自动经 WebSocket 推送 artifact_new）。"""
    import file_knowledge
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


async def run_sprint(
    requirement: str,
    uploads_text: str = "",
    fast: bool = False,
    uploads_images: list | None = None,
    direct_artifact: bool = False,
    upload_ids: list[str] | None = None,
    task_kind: str = "auto",
) -> None:
    """驱动一轮 Sprint：直生成品或走多节点工作流，收尾时落库用量与状态。"""
    import file_knowledge
    from requirement_contract import build_contract
    sprint_id = sprint_meta["id"]
    stop_calls = threading.Event()
    budget = float(sprint_meta.get("budget") or 0)
    baseline = float(sprint_meta.get("usage_baseline") or 0)

    from route_helpers import _local_model_ready

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
        llm.SCENE_PACE = runtime_state.FAST_SCENE_PACE if fast else runtime_state.BASE_SCENE_PACE
        bus.emit({"type": "sprint_start", "requirement": requirement, "sprint_id": sprint_id})
        ticker_task = asyncio.create_task(cost_ticker())
        if direct_artifact:
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
        result = await runtime_state.graph.ainvoke(
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
        runtime_state.running = False
        sprint_meta["finished_at"] = sprint_meta["finished_at"] or time.time()
        llm.SCENE_PACE = runtime_state.BASE_SCENE_PACE
        forge.set_explicit_save_dir("")
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
        # 兜底终态信号（每轮运行的最后一个事件）：无论 sprint_done/stopped/error
        # 是否送达前端，前端都以它收敛运行状态，避免面板卡在「进行中」。
        bus.emit(
            {
                "type": "sprint_end",
                "sprint_id": sprint_id,
                "status": sprint_meta.get("status", ""),
                "stage": sprint_meta.get("stage", ""),
            }
        )


async def _run_demo() -> None:
    """零成本演示模式：脚本化回放一场完整 Sprint。"""
    import demo
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
        runtime_state.running = False
        # 与 run_sprint 相同：演示模式也补发兜底终态信号
        bus.emit(
            {
                "type": "sprint_end",
                "sprint_id": sprint_meta.get("id", ""),
                "status": sprint_meta.get("status", ""),
                "stage": sprint_meta.get("stage", ""),
            }
        )
