"""Durable node checkpoints and explicit recovery plans; never starts paid work on boot."""
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
import json
import time
import os
from functools import lru_cache

from langchain_core.runnables import RunnableConfig

import forge
import storage
import runtime_state

SCHEMA = 1
NODES = {"kickoff": "需求拆解", "assign": "任务分派", "develop": "分工开发", "candidate": "制作候选文件",
         "test": "测试验证", "review": "技术评审", "fix": "缺陷修复", "publish": "发布交付", "wrapup": "复盘收尾"}


def owner() -> dict:
    import psutil
    return {"pid": os.getpid(), "created_at": psutil.Process().create_time()}


def owner_alive(options: dict) -> bool:
    import psutil
    previous = options.get("owner") or {}
    if not previous:
        return False
    try:
        return abs(psutil.Process(int(previous["pid"])).create_time() - float(previous["created_at"])) < .01
    except psutil.NoSuchProcess:
        return False
    except (KeyError, ValueError, TypeError, psutil.AccessDenied):
        return True


def digest(path) -> str:
    value = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


@lru_cache(maxsize=1)
def signature() -> str:
    import importlib.metadata
    root = Path(__file__).parent
    value = hashlib.sha256()
    for source in sorted(root.glob("*.py")) + sorted((root / "routers").glob("*.py")):
        if not source.name.startswith("test_"):
            value.update(source.name.encode())
            value.update(source.read_bytes())
    for package in ("langgraph", "langgraph-checkpoint-sqlite"):
        value.update(importlib.metadata.version(package).encode())
    return value.hexdigest()


def durable_node(function):
    async def execute(state, config: RunnableConfig):
        sid = config["configurable"]["thread_id"]
        apply_controls(sid, state)
        from events import tasks_store
        tasks_store[:] = state.get("tasks", [])
        result = await function(state)
        files = []
        for artifact in storage.list_artifacts(sid):
            if artifact.get("file_name"):
                path = forge.safe_deliverable_path(artifact["file_name"])
                if path:
                    files.append({"artifact_id": artifact["id"], "sha256": digest(path)})
        sources = []
        merged = {**state, **result}
        import contributions
        for task in merged.get("tasks", []):
            if task.get("contribution_ref"):
                valid, _, manifest = contributions.inspect(sid, task["id"])
                if valid:
                    for entry in manifest.get("files", []):
                        source = Path(entry["path"]).resolve()
                        relative = source.relative_to(Path(forge.WORKSPACE_DIR).resolve())
                        sources.append({"path": relative.as_posix(), "sha256": digest(source)})
        candidate = storage.get_artifact(merged.get("candidate_id") or "") or {}
        project = (candidate.get("regen") or {}).get("project_dir")
        projects = []
        if project:
            import project_agent
            directory = Path(project).resolve()
            if not directory.is_dir():
                raise RuntimeError("候选项目目录缺失，不能保存可恢复检查点")
            project_files = []
            for source in project_agent.project_files(Path(project)):
                relative = source.resolve().relative_to(Path(forge.WORKSPACE_DIR).resolve())
                sources.append({"path": relative.as_posix(), "sha256": digest(source)})
                project_files.append(source.resolve().relative_to(directory).as_posix())
            projects.append({"directory": directory.relative_to(Path(forge.WORKSPACE_DIR).resolve()).as_posix(),
                             "files": sorted(project_files)})
        from event_broadcaster import snapshot_instructions
        return {"tasks": state.get("tasks", []), **result, "resume_files": files, "resume_sources": sources,
                "resume_projects": projects, "pending_instructions": snapshot_instructions(),
                "instruction_cutoff": time.time()}
    execute.__name__ = function.__name__
    return execute


def apply_controls(sprint_id: str, state: dict) -> None:
    for control in storage.task_controls_since(sprint_id, float(state.get("instruction_cutoff") or 0)):
        task = next((item for item in state.get("tasks", []) if item["id"] == control.get("task_id")), None)
        if task is not None:
            if control.get("action") == "cancel":
                task["status"] = "已取消"
            elif control.get("action") == "reassign":
                task["assignee_roster"] = control["roster"]


def activate(sprint: dict, state: dict, options: dict) -> None:
    import llm
    import events as bus
    import artifacts
    from event_broadcaster import restore_instructions, reset_speak_llm
    sid = sprint["id"]
    bus.set_paused(False)
    bus.sprint_meta.update(id=sid, requirement=sprint["requirement"], status="running",
                          stage="从检查点续跑", stage_started=time.time(), started_at=sprint["started_at"],
                          finished_at=0, deliver_kind=sprint.get("deliver_kind", ""),
                          budget=sprint.get("budget", 0), cost=sprint.get("usage_cost", 0),
                          fast=bool(options.get("fast")), execution_mode=sprint.get("execution_mode", "cloud"),
                          execution_style="collaborative", usage_baseline=llm.usage_total_cost(),
                          usage_baseline_calls=llm.usage_total_calls(), usage_baseline_tokens=llm.usage_total_tokens())
    apply_controls(sid, state)
    bus.tasks_store[:] = state.get("tasks", [])
    artifacts.set_sprint(sid)
    forge.begin_sprint(sid)
    forge.set_explicit_save_dir(str(options.get("save_dir") or ""))
    pending = list(state.get("pending_instructions", []))
    pending += storage.instructions_since(sid, float(state.get("instruction_cutoff") or 0))
    restore_instructions(pending)
    reset_speak_llm()
    bus.load_persisted(sid)
    storage.update_sprint(sid, stage="从检查点续跑")


@asynccontextmanager
async def persistent_graph():
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from graph import build_sprint_graph
    async with aiosqlite.connect(storage.DB_PATH, timeout=10) as connection:
        saver = AsyncSqliteSaver(connection, serde=JsonPlusSerializer(
            allowed_json_modules=[], allowed_msgpack_modules=[]))
        await saver.setup()
        yield build_sprint_graph(saver, durable=True)


def validate_files(state: dict) -> list[str]:
    problems = []
    for entry in state.get("resume_files", []):
        artifact = storage.get_artifact(entry["artifact_id"])
        path = forge.safe_deliverable_path(artifact.get("file_name")) if artifact and artifact.get("file_name") else None
        if not path or digest(path) != entry["sha256"]:
            problems.append(f"产物 {entry['artifact_id']} 缺失或已修改，不能沿用旧检查点")
    root = Path(forge.WORKSPACE_DIR).resolve()
    for entry in state.get("resume_sources", []):
        source = (root / entry["path"]).resolve()
        if not source.is_relative_to(root) or not source.is_file() or digest(source) != entry["sha256"]:
            problems.append(f"源码或角色贡献 {entry['path']} 缺失或已修改")
    for project in state.get("resume_projects", []):
        import project_agent
        directory = (root / project["directory"]).resolve()
        if not directory.is_relative_to(root) or not directory.is_dir():
            problems.append("候选代码目录缺失或越界")
        elif sorted(source.relative_to(directory).as_posix() for source in project_agent.project_files(directory)) != project["files"]:
            problems.append("候选代码目录的文件清单已变化")
    return problems


async def plan(sprint_id: str) -> dict:
    sprint = storage.get_sprint(sprint_id)
    if not sprint:
        from fastapi import HTTPException
        raise HTTPException(404, "Sprint 不存在")
    result = {"sprint_id": sprint_id, "requirement": sprint["requirement"], "status": sprint["status"],
              "resumable": False, "checkpoint_id": "", "next_nodes": [], "next_steps": [],
              "spent": float(sprint.get("usage_cost") or 0), "budget": float(sprint.get("budget") or 0),
              "warning": "已保存的完成节点不会重跑；中断节点可能重新生成文件并产生模型费用。"}
    options = sprint.get("run_options") or {}
    if not runtime_state.persistent_ready:
        return {**result, "reason": "持久化工作流尚未启动，请检查后端依赖"}
    if options.get("schema") != SCHEMA or sprint.get("execution_style") != "collaborative":
        return {**result, "reason": "此任务没有兼容的续跑记录；旧任务和快速生成不能从检查点续跑"}
    if options.get("workflow_signature") != signature():
        return {**result, "reason": "后端工作流代码或依赖版本已变化，旧检查点不能安全续跑"}
    if sprint["status"] not in ("interrupted", "failed"):
        return {**result, "reason": "只有中断或执行失败的任务可续跑"}
    if owner_alive(options):
        return {**result, "reason": "原执行进程尚未退出，请重启后端并等待原进程结束后再续跑"}
    try:
        state = await runtime_state.graph.aget_state({"configurable": {"thread_id": sprint_id}})
        if not state.values or state.values.get("requirement") != sprint["requirement"]:
            return {**result, "reason": "检查点缺失或与原始需求不一致"}
        result["checkpoint_id"] = state.config["configurable"]["checkpoint_id"]
        result["next_nodes"] = list(state.next)
        result["next_steps"] = [NODES.get(node, node) for node in state.next] or ["同步最终状态"]
        if not state.next and sprint["status"] == "failed":
            return {**result, "reason": "工作流已结束但成品未通过验收，请修改产物或新建任务"}
        if any(node not in NODES for node in state.next):
            return {**result, "reason": "检查点包含不兼容的工作流步骤"}
        import asyncio
        problems = await asyncio.to_thread(validate_files, state.values)
        if problems:
            return {**result, "reason": "；".join(problems[:5])}
        if state.next and result["budget"] and result["spent"] >= result["budget"]:
            return {**result, "reason": "原任务预算已用完，不能继续调用模型"}
        return {**result, "resumable": True, "reason": "可从已保存的节点边界继续"}
    except Exception:
        return {**result, "reason": "无法读取或验证检查点；未执行任何模型调用"}
