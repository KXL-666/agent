"""Sprint 产物存储：内存保留当前 Sprint，SQLite 持久化全部历史

- 当前 Sprint 的产物列表供工作流内部汇总使用（artifacts_store）
- 每轮 Sprint 独立入库，不再在新任务开始时清空历史
- 新增/更新时通过事件总线推送，前端实时更新
"""

import threading
import time

import storage
import forge
from events import emit

_lock = threading.Lock()

# 当前 Sprint 的产物（内存快照，供交付环节汇总）
artifacts_store: list[dict] = []
current_sprint_id: str = ""


def set_sprint(sprint_id: str) -> None:
    """切换到指定 Sprint，并从数据库加载其产物（原地更新，避免其他模块持有旧引用）"""
    global current_sprint_id
    with _lock:
        current_sprint_id = sprint_id
        artifacts_store[:] = storage.list_artifacts(sprint_id) if sprint_id else []


def clear_current_sprint() -> None:
    """清空当前 Sprint 的内存快照（不影响已入库的历史）"""
    with _lock:
        artifacts_store[:] = []


def add_artifact(
    kind: str,
    title: str,
    author_roster: int | None,
    content: str,
    task_id: str | None = None,
    file_name: str | None = None,
    degraded: bool = False,
    degraded_reason: str = "",
    source: str = "model",
    quality_status: str = "unchecked",
    regen: dict | None = None,
    parent_id: str | None = None,
    version: int = 1,
    sprint_id: str | None = None,
) -> dict:
    """新增一条产物并广播；content 会被裁剪到安全长度

    degraded 表示内容来自兜底/Mock 或真实文件生成失败，前端会显示警示徽标；
    regen 保存重新生成所需的参数（类型/spec/文件名/成员），供「重试」使用；
    sprint_id 为空时写入当前 Sprint（历史 Sprint 修订时显式传入）。"""
    with _lock:
        sid = sprint_id or current_sprint_id
        if file_name and kind in ("final", "custom"):
            requirement = (storage.get_sprint(sid) or {}).get("requirement", "") if sid else ""
            file_name = forge.publish_file(file_name, sid, project_name=requirement)
        if file_name:
            import artifact_quality
            path = forge.safe_deliverable_path(file_name)
            report = artifact_quality.load_report(path, forge.WORKSPACE_DIR) if path else None
            if report:
                # Structural checks may veto a release, but cannot promote an
                # unreviewed revision to an approved collaborative artifact.
                if report.get("status") == "failed":
                    quality_status = "failed"
                    degraded = True
                    degraded_reason = degraded_reason or artifact_quality.feedback(report)[:500]
        artifact = {
            "id": storage.next_artifact_id(sid) if sid else f"A{int(time.time() * 1000)}",
            "sprint_id": sid,
            "kind": kind,
            "title": title,
            "author_roster": author_roster,
            "task_id": task_id,
            "content": (content or "").strip()[:16000] or "（暂无内容）",
            "file_name": file_name,
            "created_at": time.time(),
            "degraded": bool(degraded),
            "degraded_reason": degraded_reason,
            "source": source,
            "quality_status": quality_status,
            "regen": regen,
            "parent_id": parent_id,
            "version": version,
        }
        if sid:
            storage.add_artifact(sid, artifact)
        if sid == current_sprint_id:
            artifacts_store.append(artifact)
    emit({"type": "artifact_new", "artifact": artifact, "sprint_id": sid})
    return artifact


def update_artifact(artifact_id: str, **fields) -> dict | None:
    """更新产物（重试/修订后刷新内容），并同步内存快照与前端"""
    old = storage.get_artifact(artifact_id)
    if fields.get("file_name") and old and old.get("kind") in ("final", "custom"):
        sid = old.get("sprint_id", "")
        requirement = (storage.get_sprint(sid) or {}).get("requirement", "") if sid else ""
        fields["file_name"] = forge.publish_file(fields["file_name"], sid, project_name=requirement)
    if fields.get("file_name"):
        import artifact_quality
        path = forge.safe_deliverable_path(fields["file_name"])
        report = artifact_quality.load_report(path, forge.WORKSPACE_DIR) if path else None
        if report and report.get("status") == "failed":
            fields["quality_status"] = "failed"
    updated = storage.update_artifact(artifact_id, **fields)
    if not updated:
        return None
    with _lock:
        for index, item in enumerate(artifacts_store):
            if item["id"] == artifact_id:
                artifacts_store[index] = updated
                break
    emit({"type": "artifact_update", "artifact": updated, "sprint_id": updated.get("sprint_id", "")})
    return updated


def get_artifact(artifact_id: str) -> dict | None:
    return storage.get_artifact(artifact_id)
