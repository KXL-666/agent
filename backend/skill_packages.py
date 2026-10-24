"""Install complete SKILL.md directories from GitHub or ZIP and run declared scripts.

Each package keeps its assets and dependencies in backend/workspace/skills. Adapters
report unsupported requirements instead of silently treating a copied file as ready.
"""
from __future__ import annotations

import io
import hashlib
from collections.abc import Callable
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, quote, unquote, urlparse
import uuid
import venv
import zipfile

import httpx

import skills

PACKAGE_ROOT = Path(__file__).parent / "workspace" / "skills"
MAX_ARCHIVE_BYTES = 30 * 1024 * 1024
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_FILES = 600
SCRIPT_SUFFIXES = {".py": "python", ".js": "node", ".mjs": "node"}
UNSUPPORTED_SCRIPTS = {".sh", ".bash", ".ps1", ".bat", ".cmd", ".exe", ".wasm", ".rb", ".go", ".rs"}
UNSUPPORTED_MANIFESTS = {"pyproject.toml", "environment.yml", "environment.yaml", "Cargo.toml", "go.mod",
                         "mcp.json", ".mcp.json", "Dockerfile"}


def _public_https(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("技能来源必须是公开的 HTTPS 地址")
    host = parsed.hostname
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("不允许从本机或内网下载技能")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        addresses = [ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(host, 443)]
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("不允许从本机或内网下载技能")


def _download(url: str) -> bytes:
    for _ in range(4):
        _public_https(url)
        with httpx.stream("GET", url, timeout=45, follow_redirects=False,
                          headers={"User-Agent": "AgileDevTeam-SkillInstaller/1"}) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                from urllib.parse import urljoin
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_ARCHIVE_BYTES:
                    raise ValueError("技能压缩包超过 30 MB")
            return bytes(content)
    raise ValueError("技能下载重定向次数过多")


def _github_archive(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.hostname not in {"github.com", "www.github.com"}:
        return url, ""
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError("GitHub 地址需包含所有者和仓库名")
    if parsed.path.lower().endswith(".zip"):
        return url, ""
    owner, repo = parts[:2]
    repo = repo.removesuffix(".git")
    if not all(re.fullmatch(r"[\w.-]+", value) for value in (owner, repo)):
        raise ValueError("GitHub 仓库地址无效")
    if len(parts) >= 4 and parts[2] == "tree":
        ref = parse_qs(parsed.query).get("ref", [parts[3]])[0]
        tree_path = "/".join(parts[3:])
        if not tree_path.startswith(ref):
            raise ValueError("GitHub 分支与目录不匹配")
        subdir = tree_path[len(ref):].lstrip("/")
    elif len(parts) == 2:
        ref, subdir = "HEAD", ""
    else:
        raise ValueError("请提供 GitHub 仓库或 tree/分支/技能目录地址")
    return f"https://api.github.com/repos/{owner}/{repo}/zipball/{quote(ref, safe='')}", subdir


def _unpack(data: bytes, destination: Path) -> Path:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise ValueError("下载内容不是有效 ZIP 技能包") from error
    entries = archive.infolist()
    if len(entries) > MAX_FILES or sum(item.file_size for item in entries) > MAX_UNPACKED_BYTES:
        raise ValueError("技能包文件数量或解压体积超限")
    destination.mkdir(parents=True, exist_ok=True)
    for item in entries:
        name = item.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or ":" in name:
            raise ValueError("技能包包含越界路径")
        if stat.S_IFMT(item.external_attr >> 16) == stat.S_IFLNK:
            raise ValueError("技能包不允许符号链接")
        target = destination.joinpath(*path.parts)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(item) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) == 1 and len(list(destination.iterdir())) == 1:
        return roots[0]
    return destination


def _venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _run(command: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _prepare_python(root: Path) -> list[str]:
    requirements = next((p for p in (root / "requirements.txt", root / "scripts" / "requirements.txt") if p.is_file()), None)
    try:
        venv.EnvBuilder(with_pip=True).create(root / ".venv")
        if requirements:
            result = _run([str(_venv_python(root)), "-m", "pip", "install", "--only-binary=:all:",
                           "--disable-pip-version-check", "-r", str(requirements)], root, 180)
            if result.returncode:
                return ["Python 依赖安装失败：" + result.stderr[-500:]]
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
        return ["Python 环境创建失败：" + str(error)[:300]]
    return []


def _prepare_node(root: Path) -> list[str]:
    if not shutil.which("node"):
        return ["缺少 Node.js 运行环境"]
    package_json = next((p for p in (root / "package.json", root / "scripts" / "package.json") if p.is_file()), None)
    if not package_json:
        return []
    npm = shutil.which("npm")
    if not npm:
        return ["缺少 npm"]
    try:
        result = _run([npm, "install", "--ignore-scripts", "--no-audit", "--no-fund"], package_json.parent, 180)
        if result.returncode:
            return ["Node 依赖安装失败：" + result.stderr[-500:]]
    except (OSError, subprocess.TimeoutExpired) as error:
        return ["Node 依赖安装失败：" + str(error)[:300]]
    return []


RUNTIME_ADAPTERS: dict[str, tuple[Callable[[Path], list[str]], Callable[[Path], str | None]]] = {
    "python": (_prepare_python, lambda root: str(_venv_python(root))),
    "node": (_prepare_node, lambda _root: shutil.which("node")),
}


def _prepare_runtime(root: Path) -> tuple[list[dict], list[str]]:
    """Install known dependency manifests without lifecycle hooks; retain unknown needs."""
    tools = []
    problems = []
    scripts = root / "scripts"
    candidates = list(scripts.rglob("*")) if scripts.is_dir() else []
    candidates.extend(path for path in root.iterdir() if path.is_file() and path.stem in {"main", "cli", "run"})
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            if path.suffix in SCRIPT_SUFFIXES:
                tools.append({"path": path.relative_to(root).as_posix(), "runtime": SCRIPT_SUFFIXES[path.suffix]})
            elif path.suffix.lower() in UNSUPPORTED_SCRIPTS:
                problems.append("尚无运行适配器：" + path.relative_to(root).as_posix())
    for name in UNSUPPORTED_MANIFESTS:
        if (root / name).is_file():
            problems.append("尚无依赖适配器：" + name)
    instructions = (root / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    for path in root.iterdir():
        if path.is_file() and path.suffix in SCRIPT_SUFFIXES and path not in candidates and path.name in instructions:
            problems.append("未注册脚本入口：" + path.name)
    referenced = set(re.findall(r"\b(?:scripts|references|assets|templates)/[\w./-]+", instructions))
    for relative in sorted(referenced):
        if not (root / relative).exists():
            problems.append("技能引用的文件缺失：" + relative)
    for runtime in sorted({tool["runtime"] for tool in tools}):
        problems.extend(RUNTIME_ADAPTERS[runtime][0](root))
    manifest = root / "skill-package.json"
    if manifest.is_file():
        try:
            extra = json.loads(manifest.read_text(encoding="utf-8"))
            required = extra.get("commands", [])
            if not isinstance(required, list) or any(not isinstance(x, str) for x in required):
                raise ValueError("commands 应为命令名列表")
            problems.extend(f"缺少外部工具：{name}" for name in required if not shutil.which(name))
            problems.extend("尚无能力适配器：" + key for key in extra
                            if key not in {"commands", "name", "description", "version"})
        except (ValueError, json.JSONDecodeError) as error:
            problems.append("技能依赖声明无效：" + str(error)[:200])
    return tools, problems


def install(source: str | bytes, roster: int, skill_path: str = "") -> list[dict]:
    """Install every SKILL.md in a package, or one selected GitHub directory."""
    if roster not in range(1, 7):
        raise ValueError("成员必须在 1–6")
    url = source.split("?", 1)[0] if isinstance(source, str) else "上传的 ZIP"
    archive_url, github_path = _github_archive(source) if isinstance(source, str) else ("", "")
    data = _download(archive_url) if isinstance(source, str) else source
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("技能压缩包超过 30 MB")
    PACKAGE_ROOT.mkdir(parents=True, exist_ok=True)
    installed = []
    try:
        with tempfile.TemporaryDirectory(prefix="skill-unpack-") as temp:
            base = _unpack(data, Path(temp))
            selected = skill_path or github_path
            if selected:
                target = (base / selected).resolve()
                if not target.is_relative_to(base.resolve()) or not target.is_dir():
                    raise ValueError("技能目录不存在或越界")
                base = target
            skill_files = [base / "SKILL.md"] if (base / "SKILL.md").is_file() else sorted(base.rglob("SKILL.md"))
            if not skill_files:
                raise ValueError("压缩包中未找到 SKILL.md")
            for skill_file in skill_files:
                package_id = uuid.uuid4().hex
                destination = PACKAGE_ROOT / package_id
                shutil.copytree(skill_file.parent, destination, ignore=shutil.ignore_patterns(".git", "node_modules", ".venv", "__pycache__"))
                parsed = skills.parse_markdown((destination / "SKILL.md").read_text(encoding="utf-8"))
                tools, problems = _prepare_runtime(destination)
                item = skills.save_skill({**parsed, "rosters": [roster], "enabled": not problems,
                                          "package_id": package_id, "source": url,
                                          "tools": tools, "install_status": "ready" if not problems else "needs_attention",
                                          "install_issues": problems})
                installed.append(item)
    except Exception:
        for item in installed:
            skills.delete_skill(item["id"])
        if "destination" in locals() and destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        raise
    return installed


def _member_package(skill_id: str, roster: int) -> tuple[dict, Path]:
    item = next((s for s in skills.list_skills() if s["id"] == skill_id and roster in s["rosters"]), None)
    if not item or not item.get("package_id"):
        raise ValueError("该成员没有此技能包")
    root = (PACKAGE_ROOT / item["package_id"]).resolve()
    if not root.is_dir() or not root.is_relative_to(PACKAGE_ROOT.resolve()):
        raise ValueError("技能包文件不存在")
    return item, root


def _asset_path(root: Path, path: str) -> Path:
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file() or any(
        part in {".venv", "node_modules", ".git"} for part in target.relative_to(root).parts
    ):
        raise ValueError("技能附件不存在")
    return target


def list_files(skill_id: str, roster: int) -> list[dict]:
    _, root = _member_package(skill_id, roster)
    files = []
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in {".venv", "node_modules", ".git"}
                          and not (Path(current) / name).is_symlink()]
        for name in names:
            path = Path(current) / name
            if path.is_file() and not path.is_symlink():
                files.append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size})
                if len(files) >= MAX_FILES:
                    return files
    return files


def asset_file(skill_id: str, roster: int, path: str) -> Path:
    _, root = _member_package(skill_id, roster)
    return _asset_path(root, path)


def read_asset(skill_id: str, roster: int, path: str) -> dict:
    target = asset_file(skill_id, roster, path)
    data = target.read_bytes()
    if len(data) <= 30000:
        try:
            return {"path": path, "text": data.decode("utf-8")}
        except UnicodeDecodeError:
            pass
    return {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "note": "二进制或大附件可由技能脚本使用，也可通过文件接口下载"}


def repair(skill_id: str) -> dict:
    item = next((s for s in skills.list_skills() if s["id"] == skill_id), None)
    if not item or not re.fullmatch(r"[0-9a-f]{32}", str(item.get("package_id", ""))):
        raise ValueError("技能包不存在")
    root = (PACKAGE_ROOT / item["package_id"]).resolve()
    if not root.is_dir() or not root.is_relative_to(PACKAGE_ROOT.resolve()):
        raise ValueError("技能包文件不存在")
    tools, issues = _prepare_runtime(root)
    return skills.update_package_status(skill_id, tools, issues)


def run_tool(skill_id: str, roster: int, script: str, arguments: list[str] | None = None) -> dict:
    item, root = _member_package(skill_id, roster)
    if not item.get("enabled") or item.get("install_status") != "ready":
        raise ValueError("该成员没有可运行的技能包")
    tool = next((t for t in item.get("tools", []) if t["path"] == script), None)
    if not tool:
        raise ValueError("脚本未在技能包中注册")
    path = (root / script).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("技能脚本不存在")
    args = arguments or []
    if not isinstance(args, list) or len(args) > 20 or any(not isinstance(arg, str) or len(arg) > 2000 for arg in args):
        raise ValueError("脚本参数过多或过长")
    adapter = RUNTIME_ADAPTERS.get(tool["runtime"])
    if not adapter:
        raise ValueError("该脚本运行适配器不可用")
    executable = adapter[1](root)
    if not executable or not Path(executable).is_file():
        raise ValueError("技能运行环境不可用")
    before = {entry["path"] for entry in list_files(skill_id, roster)}
    try:
        result = _run([executable, str(path), *args], root, 60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "output": "技能脚本运行超过 60 秒"}
    after = {entry["path"] for entry in list_files(skill_id, roster)}
    return {"ok": result.returncode == 0, "exit_code": result.returncode,
            "output": (result.stdout + "\n" + result.stderr)[-12000:],
            "files_created": sorted(after - before)}
