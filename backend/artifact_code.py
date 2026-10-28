"""代码类产物：LLM 直接写源文件、按 Sprint 隔离目录、静态语法检查与项目整合。

只落盘不执行生成代码，真实运行验证交给隔离执行器（project_agent）。
CODE_ROOT / CODE_DIR 由 forge 持有（运行时可被测试或部署覆盖），这里每次读取
forge 的当前值，避免两套常量各说各话导致文件写错位置。
"""

import json
import os
import py_compile
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from llm import chat_text
from artifact_naming import semantic_stem
from artifact_content import meta_leaks
from path_manager import _workspace_dir


def _code_root() -> str:
    import forge
    return forge.CODE_ROOT


def _code_dir() -> str:
    import forge
    return forge.CODE_DIR


def _set_code_dir(path: str) -> None:
    import forge
    forge.CODE_DIR = path


_CODE_FILE_PATTERN = re.compile(r"===FILE:\s*([^\n]+)\n(.*?)===END", re.DOTALL)


def _parse_code_files(raw: str) -> list[tuple[str, str]]:
    """从 ===FILE/===END 标记文本中提取 (相对路径, 内容) 列表"""
    return [
        (match.group(1).strip(), match.group(2).strip("\n"))
        for match in _CODE_FILE_PATTERN.finditer(raw or "")
    ]


def forge_code(task_title: str, requirement: str, subdir: str = "", roster: int | None = None,
               generation_meta: dict | None = None) -> list[str]:
    """让 LLM 为开发任务直接产出真实代码文件，落盘到 deliverables/code/；返回相对路径列表"""
    code_dir = _code_dir()
    target_dir = os.path.join(code_dir, subdir) if subdir else code_dir
    os.makedirs(target_dir, exist_ok=True)

    raw = chat_text(
        "你是资深全栈工程师。根据开发任务产出真实、可运行的代码。",
        [{"role": "user", "content": (
            f"所属需求：{requirement}\n开发任务：{task_title}\n\n"
            "输出 1-4 个完整代码文件，每个文件严格使用如下格式（不要输出任何其他解释文字）：\n"
            "===FILE: 相对路径/文件名\n文件完整内容\n===END\n"
            "要求：代码完整可运行、带必要注释、贴近工程实践；严格遵循用户消息中的角色文件边界，"
            "只输出本任务允许的文件，不要复刻其他成员负责的职责或提交一份完整重复项目；"
            "path 只能是相对路径，不允许 .. 或绝对路径。"
        )}],
        fallback="",
        temperature=0.4,
        max_tokens=8000,
        timeout=300,
        roster=roster, meta=generation_meta, tool_mode=False,
        fallback_roster=1 if roster != 1 else None,
    )
    files = _parse_code_files(raw)
    if not files:
        print(f"[Forge] 代码标记解析失败，进行一次格式修复：{task_title}")
        retry_meta: dict = {}
        raw = chat_text(
            "你是代码文件格式修复器。只输出完整文件，不要写解释。每个文件严格使用\n"
            "===FILE: 相对路径/文件名\n完整文件内容\n===END\n格式，至少输出一个文件。",
            [{"role": "user", "content": json.dumps({
                "task": task_title,
                "requirements_and_role_boundary": requirement,
                "previous_response": (raw or "（空响应）")[:4000],
                "instruction": "重新生成本任务所需文件；仅返回上述标记格式。",
            }, ensure_ascii=False)}],
            fallback="", temperature=0.2, max_tokens=8000, timeout=300,
            roster=roster, meta=retry_meta, tool_mode=False,
            fallback_roster=1 if roster != 1 else None,
        )
        if generation_meta is not None:
            generation_meta.update({key: value for key, value in retry_meta.items() if value})
        files = _parse_code_files(raw)
        if not files:
            return []

    written: list[str] = []
    for rel, content in files[:4]:
        rel = rel.strip().lstrip("/\\").replace("\\", "/")
        if not rel or ".." in rel.split("/") or not content.strip() or meta_leaks(content):
            continue
        parts = [part.lower() for part in rel.split("/")]
        name = parts[-1]
        is_test_file = (any(part in ("test", "tests") for part in parts[:-1])
                        or bool(re.match(r"(?:test_.*|.*_test|.*\.test|.*\.spec)\.[^.]+$", name)))
        if roster == 4 and not is_test_file:
            print(f"[Forge] QA 贡献越界，已拒绝非测试文件：{rel}")
            continue
        if roster in (2, 3) and is_test_file:
            print(f"[Forge] 开发贡献越界，测试文件应由 QA 负责：{rel}")
            continue
        abs_path = os.path.realpath(os.path.join(target_dir, rel))
        if os.path.commonpath([abs_path, os.path.realpath(target_dir)]) != os.path.realpath(target_dir):
            continue
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            written.append("_process/" + os.path.relpath(abs_path, _workspace_dir()).replace("\\", "/"))
        except OSError as error:
            print(f"[Forge] 代码落盘失败 {rel}：{error}")
    return written


def begin_sprint(sprint_id: str) -> str:
    """切换当前 Sprint 的代码目录：每轮独立存放，历史代码不会被新任务删除"""
    safe_id = re.sub(r"[^\w-]", "", sprint_id or "") or "current"
    directory = os.path.join(_code_root(), safe_id)
    os.makedirs(directory, exist_ok=True)
    _set_code_dir(directory)
    return directory


def sprint_code_dir(sprint_id: str) -> str:
    safe_id = re.sub(r"[^\w-]", "", sprint_id or "") or "current"
    return os.path.join(_code_root(), safe_id)


def verify_generated_code(code_dir: str | None = None) -> list[str]:
    """对产出代码做基础可运行性检查：Python 语法编译 + Node 语法检查"""
    failures: list[str] = []
    root = Path(code_dir or _code_dir())
    for py_file in root.rglob("*.py"):
        try:
            py_compile.compile(
                str(py_file),
                cfile=os.path.join(tempfile.gettempdir(), f"check_{uuid.uuid4().hex}.pyc"),
                doraise=True,
            )
        except py_compile.PyCompileError as error:
            failures.append(f"{py_file.name}: {str(error).splitlines()[-1][:60]}")
    node = shutil.which("node")
    if node:
        for js_file in list(root.rglob("*.js")) + list(root.rglob("*.mjs")):
            try:
                proc = subprocess.run(
                    [node, "--check", str(js_file)],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    encoding="utf-8",
                    errors="replace",
                )
                if proc.returncode != 0:
                    failures.append(
                        f"{js_file.name}: {(proc.stderr or '语法检查未通过').strip()[:80]}"
                    )
            except (subprocess.TimeoutExpired, OSError):
                continue
    return failures


def selected_project(code_dir: str | None = None) -> str:
    root = Path(code_dir or _code_dir()).resolve()
    try:
        selection = json.loads((root / ".selected-project.json").read_text(encoding="utf-8"))
        chosen = (root / selection["directory"]).resolve()
        if chosen.is_relative_to(root) and chosen.is_dir():
            return str(chosen)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return str(root)


def project_report(code_dir: str | None = None) -> dict:
    root = Path(code_dir or _code_dir())
    try:
        return json.loads((root / ".project-quality.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        import project_agent
        return project_agent.evaluate(Path(selected_project(str(root))), [], "node" if (root / "package.json").is_file() else "python", execute=False)


def create_project(requirement: str, code_dir: str, roster: int | None = None, project_title: str = "") -> dict:
    import project_agent
    from llm import llm_available
    if roster is not None and not llm_available(roster):
        roster = None
    root = Path(code_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    project_name = semantic_stem(project_title or requirement, "代码项目")
    destination = root / project_name
    index = 2
    while destination.exists():
        destination = root / f"{project_name} ({index})"
        index += 1
    destination, report = project_agent.assemble(requirement, root, destination, roster)
    (root / ".selected-project.json").write_text(json.dumps({"directory": destination.relative_to(root).as_posix()}), encoding="utf-8")
    (root / ".project-quality.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def finalize_project(requirement: str) -> list[str]:
    """将角色贡献整合为完整项目；保留真实测试证据，不用通用 README 冒充工程交付。"""
    create_project(requirement, _code_dir(), 5)
    root = Path(selected_project())
    return [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
