"""路由层共享辅助函数。

拆分 main.py 时提取的跨域工具函数：本地模式检测、产物类型识别、
保存位置提示、当前 Sprint ID、产物/任务查找、成员视图等。

被多个 router 模块复用的函数放这里，避免循环依赖与重复实现。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import agent_config
import llm
import storage
from artifacts import artifacts_store
from events import sprint_meta, tasks_store
from roles import ROSTER, roster_name


def _local_model_ready() -> bool:
    """本地模式仅当所有已配置的主、备用模型 API 都位于本机回环地址时才允许。"""
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


def _detect_file_kind(prompt: str) -> str | None:
    """根据老板指令判断要生成的真实文件类型；返回 None 表示保持 Markdown。"""
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
    from llm import llm_available
    configured = [roster for roster in sorted(ROSTER) if llm_available(roster)]
    healthy = [roster for roster in configured if not llm.agent_failure(roster)]
    if preferred in healthy:
        return preferred
    return next(iter(healthy or configured), None)


def _should_direct_artifact_sprint(requirement: str) -> bool:
    """明确的单文件需求直接走生成 + 真实验收，不进多轮协作流程。"""
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
    return explicit_file and not collaborative and _detect_file_kind(requirement) in (
        "docx", "pptx", "xlsx", "pdf", "html",
    )


def _saved_note(dest: str | None, target_dir: str | None = None) -> str:
    if dest:
        return f"\n\n📁 已按你的要求保存到：{dest}"
    if target_dir:
        return f"\n\n⚠ 保存到「{target_dir}」失败：目录不可写或不存在，成品仍可在产物中心下载"
    return ""


def _current_sprint_id() -> str:
    return sprint_meta.get("id") or ""


def _sprint_view(sprint_id: str | None) -> dict | None:
    if not sprint_id:
        return None
    return storage.get_sprint(sprint_id)


def _active_round(running: bool) -> dict:
    """只有正在运行的 Sprint 才算「当前轮次」，已结束的属于历史记录。"""
    if running:
        return {"tasks": tasks_store, "artifacts": artifacts_store, "sprint": sprint_meta}
    return {"tasks": [], "artifacts": [], "sprint": {}}


def _find_task(task_id: str) -> dict:
    for task in tasks_store:
        if task.get("id") == task_id:
            return task
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="任务不存在")


def _find_artifact(artifact_id: str) -> dict:
    artifact = storage.get_artifact(artifact_id)
    if not artifact:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="产物不存在")
    return artifact


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


def build_report_md(requirement: str, tasks: list[dict], running: bool) -> str:
    lines = [
        "# Sprint 日报",
        "",
        f"- 需求：{requirement or '（未记录）'}",
        f"- 导出时间：{__import__('time').strftime('%Y-%m-%d %H:%M:%S')}",
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
