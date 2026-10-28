"""产物中心路由。

- GET    /api/artifacts/deliverables           跨 Sprint 交付物列表
- GET    /api/sprints/{id}/artifacts.zip       单轮产物打包下载
- GET    /api/artifacts/code.zip               代码打包下载
- GET    /api/artifact-capabilities            产物能力声明
- GET    /api/artifacts/{id}/download          下载产物文件
- POST   /api/artifacts/{id}/reveal            在文件管理器中定位
- GET    /api/artifacts/{id}/preview           HTML 预览
- GET    /api/artifacts/{id}/provenance        产物溯源链
- GET    /api/artifacts/{id}/quality           质量报告
- GET    /api/artifacts/{id}/quality/pages/{p} 质量预览页
- GET    /api/artifacts/{id}/versions          版本链
- DELETE /api/artifacts/{id}                   删除产物
- POST   /api/artifacts/{id}/retry             重新生成
- POST   /api/artifacts/{id}/revise            修订生成新版本
- POST   /api/artifacts/{id}/rollback          回滚到旧版本
- GET    /api/artifacts/{id}/raw               文字内容（供编辑器）
- POST   /api/artifacts/{id}/edit              站内编辑保存新版本
- GET    /api/artifacts/{id}/code_preview      HTML 沙箱预览
- POST   /api/artifacts/generate               直接生成成品
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import forge
import llm
import storage
import uploads
import artifacts as artifact_store
from artifacts import add_artifact, artifacts_store
import events as bus
from events import sprint_meta, tasks_store
from llm import llm_available
from route_helpers import (
    _current_sprint_id,
    _detect_file_kind,
    _saved_note,
    _select_artifact_roster,
)
import runtime_state
from sprint_runner import (
    _generate_document_safe,
    _manual_contribution_refs,
    _rebuild,
    _revalidate_manual_collaborative_version,
    _zip_code_for,
    _zip_files,
)


def _find_artifact(artifact_id: str) -> dict:
    """运行时查找 main._find_artifact，确保测试 patch.object(main, '_find_artifact') 生效。"""
    import main
    return main._find_artifact(artifact_id)

router = APIRouter(tags=["artifacts"])


class GenerateRequest(BaseModel):
    prompt: str
    upload_ids: list[str] = []


class RevisionRequest(BaseModel):
    instruction: str
    mode: str = "auto"


class RollbackRequest(BaseModel):
    target_id: str


class EditRequest(BaseModel):
    content: str


@router.get("/api/artifacts/deliverables")
async def list_deliverables():
    """产物中心使用的跨 Sprint 最终交付物列表。"""
    return {"artifacts": storage.list_deliverables()}


@router.get("/api/sprints/{sprint_id}/artifacts.zip")
async def download_sprint_artifacts_zip(sprint_id: str):
    """把该 Sprint 全部真实文件产物打成一个 zip 一次下载。"""
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


@router.get("/api/artifacts/code.zip")
async def download_code_zip(sprint: str | None = None):
    """打包下载指定 Sprint（默认当前）产出的全部代码。"""
    sprint_id = sprint or _current_sprint_id()
    zip_name = await asyncio.to_thread(_zip_code_for, sprint_id)
    if not zip_name:
        raise HTTPException(status_code=404, detail="该 Sprint 尚未产出代码")
    return FileResponse(
        forge.output_path(zip_name), filename=os.path.basename(zip_name)
    )


@router.get("/api/artifact-capabilities")
async def artifact_capabilities():
    import office_runtime
    import project_agent
    return {**office_runtime.capabilities(), "code_sandbox": await asyncio.to_thread(project_agent.docker_available)}


@router.get("/api/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: str):
    """下载产物对应的真实文件（仅允许 deliverables 目录内）。"""
    artifact = _find_artifact(artifact_id)
    if not artifact.get("file_name"):
        raise HTTPException(status_code=404, detail="该产物没有关联的真实文件")
    path = forge.safe_deliverable_path(artifact["file_name"])
    if not path:
        raise HTTPException(status_code=404, detail="文件不存在或已被清理")
    return FileResponse(path, filename=os.path.basename(path))


@router.post("/api/artifacts/{artifact_id}/reveal")
async def reveal_artifact(artifact_id: str):
    """在系统文件管理器中定位产物文件（仅本机自用场景）。"""
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


@router.get("/api/artifacts/{artifact_id}/preview", response_class=HTMLResponse)
async def preview_artifact(artifact_id: str):
    """Word/PPT/Excel 结构化 HTML 预览。"""
    artifact = _find_artifact(artifact_id)
    file_name = artifact.get("file_name") or ""
    body = forge.preview_html(file_name) if file_name else ""
    if file_name:
        path = forge.safe_deliverable_path(file_name)
        if path:
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


@router.get("/api/artifacts/{artifact_id}/provenance")
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


@router.get("/api/artifacts/{artifact_id}/quality")
async def artifact_quality_report(artifact_id: str):
    artifact = _find_artifact(artifact_id)
    file_name = artifact.get("file_name")
    if not file_name:
        raise HTTPException(status_code=404, detail="该产物没有真实文件")
    result = await asyncio.to_thread(forge.self_check, file_name)
    return {key: result.get(key) for key in ("kind", "status", "ok", "checks", "metrics", "review", "attempts", "pages")}


@router.get("/api/artifacts/{artifact_id}/quality/pages/{page}")
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


@router.get("/api/artifacts/{artifact_id}/versions")
async def artifact_versions(artifact_id: str):
    """版本链：返回该产物所在链条的全部版本。"""
    artifact = _find_artifact(artifact_id)
    import revision_outputs
    artifact = revision_outputs.canonical(artifact)
    artifact_id = artifact['id']
    sprint_id = artifact.get("sprint_id") or _current_sprint_id()
    root_id = storage.artifact_root(sprint_id, artifact_id)
    chain = storage.artifact_versions(sprint_id, root_id)
    return {"root_id": root_id, "versions": [item for item in chain if not (item.get('regen') or {}).get('bundle_anchor')]}


@router.delete("/api/artifacts/{artifact_id}")
async def delete_artifact(artifact_id: str):
    """删除单条产物及其磁盘文件（修订版本不连带删除）。"""
    artifact = _find_artifact(artifact_id)
    root = storage.artifact_root(artifact.get('sprint_id', ''), artifact_id)
    if storage.list_revision_sessions(root) or storage.artifact_used_by_revision(artifact_id):
        raise HTTPException(409, '该版本链用于修订会话，暂不能删除，以免丢失编辑依据')
    file_name = storage.delete_artifact(artifact_id)
    if file_name:
        path = forge.safe_deliverable_path(file_name)
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    with artifact_store._lock:
        artifact_store.artifacts_store[:] = [
            item for item in artifact_store.artifacts_store if item["id"] != artifact_id
        ]
    bus.emit({"type": "artifact_deleted", "artifact_id": artifact_id})
    return {"ok": True}


@router.post("/api/artifacts/{artifact_id}/retry")
async def retry_artifact(artifact_id: str):
    """重新生成单个产物（保留同一条记录并刷新内容）。"""
    artifact = _find_artifact(artifact_id)
    if storage.revision_operations(active=True):
        raise HTTPException(409, '产物修订仍在执行，请稍后重试')
    if (artifact.get('regen') or {}).get('mode') in ('manual_revision', 'published_candidate'):
        raise HTTPException(409, '请使用对话修改生成新版本，保留已有版本记录')
    if artifact.get("kind") == "final" and (storage.get_sprint(artifact.get("sprint_id") or "") or {}).get("execution_style") == "collaborative":
        raise HTTPException(status_code=409, detail="协作成品不能原地重试；请创建新修订版本并重新验收")
    fields = await _rebuild(artifact)
    updated = artifact_store.update_artifact(artifact_id, **fields)
    return {"ok": True, "artifact": updated}


@router.post("/api/artifacts/{artifact_id}/revise")
async def revise_artifact(artifact_id: str, req: RevisionRequest):
    from revision_service import revise
    artifact = _find_artifact(artifact_id)
    import revision_outputs
    artifact = revision_outputs.canonical(artifact)
    if (artifact.get('regen') or {}).get('bundle_members'):
        import revision_jobs
        return await revision_jobs.legacy_bundle_request(artifact, 'revise',
            {'feedback': req.instruction, 'mode': req.mode, 'artifact_id': artifact_id})
    return await revise(artifact_id, req.instruction, mode=req.mode)


@router.post("/api/artifacts/{artifact_id}/rollback")
async def rollback_artifact(artifact_id: str, req: RollbackRequest):
    from revision_service import rollback
    artifact = _find_artifact(artifact_id)
    import revision_outputs
    artifact = revision_outputs.canonical(artifact)
    artifact_id = artifact['id']
    if (artifact.get('regen') or {}).get('bundle_members'):
        import revision_jobs
        target = _find_artifact(req.target_id)
        sid = artifact['sprint_id']
        if target.get('sprint_id') != sid or storage.artifact_root(sid, target['id']) != storage.artifact_root(sid, artifact_id):
            raise HTTPException(400, '目标快照不在同一版本链')
        return await revision_jobs.legacy_bundle_request(artifact, 'rollback',
            {'target_id': req.target_id, 'feedback': '恢复历史产物组'})
    return await rollback(artifact_id, req.target_id)


@router.get("/api/artifacts/{artifact_id}/raw")
async def artifact_raw(artifact_id: str):
    """返回产物的文字内容供站内编辑器加载，上限 200KB。"""
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


@router.post("/api/artifacts/{artifact_id}/edit")
async def edit_artifact(artifact_id: str, req: EditRequest):
    if storage.revision_operations(active=True):
        raise HTTPException(409, '产物修订仍在执行，请稍后再编辑')
    """站内文本编辑：修改后的内容存为版本链上的新版本。"""
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="内容不能为空")
    if len(content) > 200_000:
        raise HTTPException(status_code=400, detail="内容超过 200KB，无法保存")
    artifact = _find_artifact(artifact_id)
    import revision_outputs
    canonical_artifact = revision_outputs.canonical(artifact)
    bundle_anchor = canonical_artifact if (canonical_artifact.get('regen') or {}).get('bundle_members') else None
    if bundle_anchor:
        import revision_jobs
        return await revision_jobs.legacy_bundle_request(bundle_anchor, 'edit',
            {'content': content, 'feedback': '保存正文编辑并同步配套文件', 'artifact_id': artifact_id})
    sprint_id = artifact.get("sprint_id") or _current_sprint_id()
    collaborative_final = (artifact.get("kind") == "final" and
                           (storage.get_sprint(sprint_id) or {}).get("execution_style") == "collaborative")
    if collaborative_final and runtime_state.running:
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


@router.get("/api/artifacts/{artifact_id}/code_preview")
async def code_preview_artifact(artifact_id: str):
    """HTML 代码产物直接以 text/html 返回，供前端 iframe sandbox 隔离预览。"""
    artifact = _find_artifact(artifact_id)
    file_name = artifact.get("file_name") or ""
    if not file_name.lower().endswith(".html"):
        raise HTTPException(status_code=400, detail="仅 HTML 产物支持沙箱预览")
    path = forge.safe_deliverable_path(file_name)
    if not path:
        raise HTTPException(status_code=404, detail="文件不存在或已被清理")
    return FileResponse(path, media_type="text/html")


@router.post("/api/artifacts/generate")
async def generate_artifact(req: GenerateRequest):
    if storage.revision_operations(active=True):
        raise HTTPException(409, '产物修订仍在执行，请稍后生成')
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    if not llm_available():
        raise HTTPException(
            status_code=400,
            detail="尚未配置任何成员的专属 API，无法生成：请先在「智能体 → ⚙ 设置」中为成员填入 API Key",
        )
    if not runtime_state.running or not _current_sprint_id():
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
        from routers.state import get_state
        bus.emit({"type": "hello", **(await get_state())})
    asyncio.create_task(_generate_document_safe(prompt, req.upload_ids, _current_sprint_id()))
    return {"ok": True, "sprint_id": _current_sprint_id(), "message": "生成任务已提交，完成后会实时出现在产物中心"}
