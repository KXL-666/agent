"""Full BE/FE source contributions with immutable hashes and integration checks."""

import ast
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re

import forge


def _root(sprint_id: str) -> Path:
    if not sprint_id or not all(char.isalnum() or char in "-_" for char in sprint_id):
        raise ValueError("无效 Sprint ID")
    return Path(forge.WORKSPACE_DIR) / "contributions" / sprint_id


def _task_id(task_id: str) -> str:
    if not task_id or not all(char.isalnum() or char in "-_" for char in task_id):
        raise ValueError("无效任务 ID")
    return task_id


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _save_manifest(sprint_id: str, task_id: str, manifest: dict) -> dict:
    root = _root(sprint_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_task_id(task_id)}.json"
    temporary = path.with_suffix(".writing")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return manifest


def save_text(sprint_id: str, task_id: str, roster: int, role_scope: str,
              content: str, requirement_ids: list[str], source_refs: list[str],
              status: str = "ready", generation_meta: dict | None = None) -> dict:
    root = _root(sprint_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_task_id(task_id)}.md"
    data = content.encode("utf-8")
    temporary = path.with_suffix(".writing")
    temporary.write_bytes(data)
    temporary.replace(path)
    manifest = {
        "schema_version": 1, "kind": "text", "status": status if status in ("ready", "unchecked", "blocked") else "unchecked",
        "sprint_id": sprint_id, "task_id": task_id,
        "roster": roster, "role_scope": role_scope, "requirement_ids": requirement_ids,
        "source_refs": source_refs, "files": [{"logical_path": f"{task_id}.md", "path": str(path), "sha256": _digest(data)}],
    }
    manifest.update(_model_fields(generation_meta))
    return _save_manifest(sprint_id, task_id, manifest)


def _model_fields(generation_meta: dict | None) -> dict:
    if not isinstance(generation_meta, dict):
        return {}
    return {key: generation_meta[key] for key in ("model", "primary_model", "fallback_roster", "fallback_reason")
            if generation_meta.get(key)}


def _code_scope_issues(roster: int, logical_paths: list[str]) -> list[dict]:
    """Mechanically enforce the QA test-only boundary and prevent devs duplicating tests."""
    issues = []
    for logical in logical_paths:
        path = PurePosixPath(logical)
        name = path.name.lower()
        is_test = ("tests" in {part.lower() for part in path.parts[:-1]}
                   or "test" in {part.lower() for part in path.parts[:-1]}
                   or bool(re.match(r"(?:test_.*|.*_test|.*\.test|.*\.spec)\.[^.]+$", name)))
        if roster == 4 and not is_test:
            issues.append({"path": logical, "reason": "QA 代码贡献只允许测试文件"})
        elif roster in (2, 3) and is_test:
            issues.append({"path": logical, "reason": "测试文件应由 QA 负责，开发角色不能重复提交测试"})
    return issues


def save_code(sprint_id: str, task_id: str, roster: int, role_scope: str,
              written: list[str], requirement_ids: list[str], source_refs: list[str],
              generation_meta: dict | None = None) -> dict:
    root = (Path(forge.sprint_code_dir(sprint_id)) / _task_id(task_id)).resolve()
    files = []
    for entry in written:
        # forge_code returns process paths; the task subdirectory is the only
        # trusted source of code for this task.
        candidate = Path(forge.WORKSPACE_DIR) / str(entry).removeprefix("_process/")
        path = candidate.resolve()
        if not path.is_file() or not path.is_relative_to(root):
            continue
        files.append({"logical_path": path.relative_to(root).as_posix(), "path": str(path),
                      "sha256": _digest(path.read_bytes())})
    scope_issues = _code_scope_issues(roster, [item["logical_path"] for item in files])
    manifest = {
        "schema_version": 1, "kind": "code", "sprint_id": sprint_id, "task_id": task_id,
        "roster": roster, "role_scope": role_scope, "requirement_ids": requirement_ids,
        "source_refs": source_refs, "files": files,
        "status": "blocked" if scope_issues or not files else "ready", "scope_issues": scope_issues,
    }
    manifest.update(_model_fields(generation_meta))
    return _save_manifest(sprint_id, task_id, manifest)


def inspect(sprint_id: str, task_id: str) -> tuple[bool, str, dict]:
    path = _root(sprint_id) / f"{_task_id(task_id)}.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "贡献清单不存在或损坏", {}
    if manifest.get("sprint_id") != sprint_id or manifest.get("task_id") != task_id:
        return False, "贡献清单与任务不匹配", manifest
    if manifest.get("kind") not in ("text", "code"):
        return False, "贡献清单类型无效", manifest
    requirement_ids = manifest.get("requirement_ids")
    if not isinstance(requirement_ids, list) or not requirement_ids or any(
        not isinstance(item, str) or not item.strip() for item in requirement_ids
    ):
        return False, "贡献未关联 PO 需求条款", manifest
    source_refs = manifest.get("source_refs")
    if not isinstance(source_refs, list) or any(not isinstance(item, str) for item in source_refs):
        return False, "贡献来源引用格式无效", manifest
    missing_sources = [key for key in requirement_ids if f"requirement:{key}" not in source_refs]
    if missing_sources:
        return False, "贡献缺少需求条款来源引用", manifest
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return False, "贡献清单缺少实际文件", manifest
    if manifest.get("status", "ready") not in ("ready", "completed"):
        details = manifest.get("scope_issues")
        return False, ("角色文件范围违规：" + json.dumps(details, ensure_ascii=False)
                       if details else "贡献生成未完成，不能作为有效交付"), manifest
    permitted = _root(sprint_id).resolve() if manifest["kind"] == "text" else Path(forge.sprint_code_dir(sprint_id)).resolve()
    for item in files:
        if not isinstance(item, dict):
            return False, "贡献文件清单格式无效", manifest
        path_value = item.get("path")
        digest = item.get("sha256")
        logical_path = item.get("logical_path")
        if not isinstance(path_value, str) or not isinstance(digest, str) or len(digest) != 64:
            return False, "贡献文件路径或哈希无效", manifest
        if not isinstance(logical_path, str) or not logical_path.strip():
            return False, "贡献文件缺少稳定路径", manifest
        logical = PurePosixPath(logical_path)
        if logical.is_absolute() or ".." in logical.parts:
            return False, "贡献逻辑路径越界", manifest
        candidate = Path(path_value).resolve()
        if not candidate.is_relative_to(permitted) or not candidate.is_file():
            return False, "贡献文件缺失或路径越界", manifest
        if _digest(candidate.read_bytes()) != digest:
            return False, "贡献文件已被改动，旧交接失效", manifest
    return True, "", manifest


def mark_completed(sprint_id: str, task_id: str) -> bool:
    """Mark a submitted contribution complete after its owning workflow step ran."""
    valid, _, manifest = inspect(sprint_id, task_id)
    if not valid:
        return False
    if manifest.get("status") == "completed":
        return True
    manifest["status"] = "completed"
    _save_manifest(sprint_id, task_id, manifest)
    return True


def scan_conflicts(sprint_id: str) -> list[dict]:
    root = _root(sprint_id)
    manifests = []
    for path in sorted(root.glob("*.json")) if root.exists() else []:
        valid, _, manifest = inspect(sprint_id, path.stem)
        if valid and manifest.get("kind") == "code":
            manifests.append(manifest)
    by_path: dict[str, list[dict]] = {}
    for manifest in manifests:
        for item in manifest["files"]:
            by_path.setdefault(item["logical_path"], []).append({**item, "task_id": manifest["task_id"]})
    issues = []
    for logical, entries in by_path.items():
        if len({item["sha256"] for item in entries}) > 1:
            issues.append({"kind": "file_conflict", "path": logical,
                           "tasks": [item["task_id"] for item in entries]})
    # Check imports only where the imported module is also a contributed
    # module. Third party and standard library imports are left unchecked.
    module_files = {Path(name).stem: entries[0] for name, entries in by_path.items()
                    if name.endswith(".py") and len(entries) == 1}
    exports = {}
    for module, item in module_files.items():
        try:
            tree = ast.parse(Path(item["path"]).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        names = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(target.id for target in node.targets if isinstance(target, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        exports[module] = names
    for module, item in module_files.items():
        try:
            tree = ast.parse(Path(item["path"]).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in exports:
                for alias in node.names:
                    if alias.name != "*" and alias.name not in exports[node.module]:
                        issues.append({"kind": "interface_mismatch", "path": item["logical_path"],
                                       "module": node.module, "symbol": alias.name, "task_id": item["task_id"]})
    return issues


def integrity_issues(sprint_id: str, task_ids: list[str]) -> list[dict]:
    issues = []
    for task_id in task_ids:
        valid, reason, _ = inspect(sprint_id, task_id)
        if not valid:
            issues.append({"kind": "missing_or_changed_contribution", "task_id": task_id, "reason": reason})
    issues.extend(scan_conflicts(sprint_id))
    return issues
