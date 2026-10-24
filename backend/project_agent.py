"""代码成品代理：项目规划、共享文件整合、静态检查与隔离运行、按错误修复。"""
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import hashlib
from artifact_content import meta_leaks

from llm import chat_json, chat_text

FORBIDDEN_PARTS = {".git", "node_modules", ".venv", "venv", "__pycache__"}
IGNORED_DIRS = FORBIDDEN_PARTS | {".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "dist", "build", "coverage", "target", "vendor"}
MAX_FILES = 40


def project_files(root: Path):
    """遍历项目自身文件，跳过依赖、构建产物和缓存目录。"""
    if not root.exists():
        return
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name.lower() not in IGNORED_DIRS
                                and not (Path(current) / name).is_symlink())
        for name in sorted(names):
            path = Path(current) / name
            if path.is_file() and not path.is_symlink():
                yield path


def safe_path(root: Path, name: str) -> Path:
    relative = name.replace("\\", "/").strip()
    if not relative or any(part in ("", ".", "..") or part in FORBIDDEN_PARTS or part.rstrip(" .") != part for part in relative.split("/")):
        raise ValueError("代码路径无效")
    if re.search(r"[:*?<>|\x00]", relative) or relative.startswith("/"):
        raise ValueError("代码文件必须使用相对路径")
    if any(re.match(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.I) for part in relative.split("/")):
        raise ValueError("文件名是系统保留名称")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("代码路径越界")
    return path


def snapshot(root: Path, limit: int = 40000) -> str:
    files, used = [], 0
    if root.exists():
        for path in project_files(root):
            if path.name.startswith("."):
                continue
            if path.stat().st_size > 150000:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            block = f"===FILE: {path.relative_to(root).as_posix()}\n{content}\n===END\n"
            if used + len(block) > limit:
                break
            files.append(block)
            used += len(block)
    return "\n".join(files)


def write_response(root: Path, raw: str, allowed: set[str] | None = None) -> list[str]:
    written = []
    for match in re.finditer(r"===FILE:\s*([^\n]+)\n(.*?)===END", raw, re.S):
        name, content = match.group(1).strip().replace("\\", "/"), match.group(2).strip("\n")
        if allowed is not None and name not in allowed:
            continue
        if len(content) > 250000 or not content.strip():
            continue
        try:
            path = safe_path(root, name)
        except ValueError:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".writing")
        temporary.write_text(content + "\n", encoding="utf-8")
        temporary.replace(path)
        written.append(name)
        if len(written) >= MAX_FILES:
            break
    return written


def merge_contributions(contributions: Path, destination: Path) -> tuple[list[dict], list[dict]]:
    """Copy nonconflicting BE/FE files verbatim before generating missing project files."""
    destination.mkdir(parents=True, exist_ok=True)
    by_logical: dict[str, list[Path]] = {}
    for task_dir in sorted(contributions.iterdir()) if contributions.is_dir() else []:
        if not task_dir.is_dir() or not re.fullmatch(r"T\d+", task_dir.name):
            continue
        for path in project_files(task_dir):
            logical = path.relative_to(task_dir).as_posix()
            try:
                safe_path(destination, logical)
            except ValueError:
                continue
            by_logical.setdefault(logical, []).append(path)
    merged, conflicts = [], []
    for logical, paths in by_logical.items():
        hashes = {hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        if len(hashes) != 1:
            conflicts.append({"path": logical, "tasks": [path.relative_to(contributions).parts[0] for path in paths]})
            continue
        target = safe_path(destination, logical)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paths[0], target)
        merged.append({"path": logical, "sha256": next(iter(hashes)),
                       "source_task": paths[0].relative_to(contributions).parts[0]})
    return merged, conflicts


def static_check(root: Path, planned: list[str] | None = None) -> list[dict]:
    checks = []
    def add(label, ok, detail):
        checks.append({"label": label, "ok": ok, "detail": str(detail)[:3000]})
    files = list(project_files(root))
    sources = [p for p in files if p.suffix in (".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".java", ".go", ".c", ".cpp", ".cs", ".rs", ".sql")]
    add("完整源代码", bool(sources), f"{len(sources)} 个源文件")
    planned = planned or []
    missing = []
    resolved_aliases = []
    has_dependency_manifest = any((root / name).is_file() for name in ("requirements.txt", "pyproject.toml"))
    test_files = [path for path in project_files(root)
                  if path.suffix == ".py" and path.parent.name.lower() in {"test", "tests"}
                  and path.name.lower().startswith("test")]
    skipped_tests = []
    skip_pattern = re.compile(
        r"(?:pytest\.(?:skip|xfail)\s*\(|pytest\.mark\.(?:skip|xfail)|"
        r"unittest\.skip(?:If|Unless)?\s*\(|self\.skipTest\s*\()"
    )
    for path in test_files:
        try:
            if skip_pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                skipped_tests.append(path.relative_to(root).as_posix())
        except OSError:
            skipped_tests.append(path.relative_to(root).as_posix())
    add("自动化测试无跳过", not skipped_tests,
        "发现 skip/xfail：" + "、".join(skipped_tests) if skipped_tests else "未发现跳过或预期失败用例")
    for name in planned:
        if safe_path(root, name).is_file():
            continue
        normalized = name.replace("\\", "/").lower()
        if normalized in {"requirements.txt", "pyproject.toml"} and has_dependency_manifest:
            resolved_aliases.append(f"{name} 由现有依赖清单替代")
            continue
        if normalized.startswith(("tests/test_", "test/test_")) and test_files:
            resolved_aliases.append(f"{name} 由 {', '.join(path.relative_to(root).as_posix() for path in test_files)} 覆盖")
            continue
        missing.append(name)
    detail = "缺失：" + "、".join(missing) if missing else "计划文件均已生成或由等价依赖/测试文件覆盖"
    if resolved_aliases:
        detail += "；" + "；".join(resolved_aliases)
    add("项目计划文件", not missing, detail)
    errors, stubs = [], []
    for source in sources:
        content = source.read_text(encoding="utf-8", errors="replace")
        if re.search(r"raise\s+NotImplementedError|\bTODO\b|Your code here|此处实现", content):
            stubs.append(source.relative_to(root).as_posix())
        if source.suffix == ".py":
            try:
                ast.parse(content, filename=str(source))
            except SyntaxError as error:
                errors.append(f"{source.relative_to(root)}:{error.lineno}: {error.msg}")
        elif source.suffix in (".js", ".mjs") and shutil.which("node"):
            checked = subprocess.run([shutil.which("node"), "--check", str(source)], capture_output=True, timeout=20,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if checked.returncode:
                errors.append(f"{source.relative_to(root)}: {checked.stderr.decode(errors='replace')[-700:]}")
    add("语法与解析", not errors, "\n".join(errors) or "Python / JavaScript 语法检查通过；TS/JSX 需后续构建验证")
    add("未完成的实现", not stubs, "、".join(stubs) or "未发现常见未实现标记")
    handoff = [(p.relative_to(root).as_posix(), issue) for p in files
               if p.suffix.lower() in (".md", ".txt", ".html", ".py", ".js", ".ts", ".tsx", ".jsx")
               for issue in meta_leaks(p.read_text(encoding="utf-8", errors="replace"))]
    add("交付内容纯净度", not handoff,
        "；".join(f"{name}: {issue}" for name, issue in handoff[:4]) or "未发现模型对话、生成说明或示例免责声明")
    add("运行与使用说明", (root / "README.md").is_file(), "需要项目入口、安装方法、配置说明和测试命令")
    readme_path = root / "README.md"
    if readme_path.is_file():
        readme = readme_path.read_text(encoding="utf-8", errors="replace")
        readme_issues = []
        requirements = root / "requirements.txt"
        has_requirements = requirements.is_file() and any(
            line.strip() and not line.lstrip().startswith(("#", "-"))
            for line in requirements.read_text(encoding="utf-8", errors="replace").splitlines()
        )
        if has_requirements and not re.search(
                r"(?:python\s+-m\s+pip|pip)\s+install\s+-r\s+requirements\.txt", readme, re.I):
            readme_issues.append("requirements.txt 有依赖，但 README 未说明 pip install -r requirements.txt")
        tests_for_docs = [p for p in files if p.name.startswith("test_") and p.suffix == ".py"]
        test_sources = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in tests_for_docs)
        pytest_style = bool(re.search(r"(?:^|\n)\s*import pytest|pytest\.mark\.", test_sources))
        if pytest_style and re.search(r"python(?:3)?\s+-m\s+unittest\b", readme):
            readme_issues.append("测试文件使用 pytest，但 README 仍提供 unittest 运行命令")

        # A package cannot normally be imported with `python -m package` after
        # changing into that package directory; the command must run at its parent.
        for block in re.findall(r"```[^\n]*\n(.*?)```", readme, re.S):
            current_dir = Path(".")
            for raw_line in block.splitlines():
                line = raw_line.strip().strip("`$").strip()
                cd = re.match(r"^(?:\$\s*)?cd\s+([^;&|]+)", line)
                if cd:
                    target = cd.group(1).strip().strip("'\" ")
                    current_dir = Path(os.path.normpath(str(current_dir / target)))
                    continue
                module = re.match(r"^(?:\$\s*)?(?:python(?:3)?|py)\s+-m\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\b", line)
                if module:
                    package_dir = Path(*module.group(1).split("."))
                    try:
                        if (root / package_dir / "__init__.py").is_file() and current_dir == package_dir:
                            readme_issues.append(
                                f"README 在包目录 {package_dir.as_posix()} 内运行 python -m {module.group(1)}；请从项目根目录运行"
                            )
                    except OSError:
                        pass
        add("README命令与依赖说明", not readme_issues,
            "；".join(readme_issues) if readme_issues else "依赖安装说明齐全，未发现包目录下的错误模块启动命令")
    tests = [p for p in files if p.name.startswith("test_") or ".test." in p.name or ".spec." in p.name or re.search(r"_test\.[a-z]+$", p.name) or p.name.endswith(("Test.java", "Tests.cs"))]
    add("自动化测试文件", bool(tests), f"{len(tests)} 个测试文件")
    test_text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in tests)
    assertions = re.findall(r"\bassert\b|\bexpect\s*\(|\bassert[A-Z]\w*|self\.assert\w+|pytest\.raises", test_text)
    add("测试中的功能断言", bool(assertions), f"发现 {len(assertions)} 处断言或异常期望")
    if re.search(r"assert\s+(?:True\b|1\s*==\s*1)", test_text):
        add("无效测试断言", False, "测试包含恒真断言，不能据此证明功能正确")
    if any(p.suffix in (".ts", ".tsx", ".jsx") for p in sources):
        add("前端依赖与构建配置", (root / "package.json").is_file(), "TypeScript/JSX 项目必须提供 package.json")
    for manifest in ("package.json", "tsconfig.json"):
        if (root / manifest).is_file():
            try:
                json.loads((root / manifest).read_text(encoding="utf-8"))
            except ValueError as error:
                add("配置文件格式", False, f"{manifest}: {error}")
    return checks


def docker_available() -> bool:
    executable = shutil.which("docker")
    if not executable:
        return False
    try:
        result = subprocess.run([executable, "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=8,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def sandbox_check(root: Path, runtime: str) -> dict:
    """只向容器暴露成品源码，不传递 API Key、宿主环境、Docker socket 或用户目录。"""
    if runtime not in ("python", "node"):
        return {"label": "隔离安装与运行测试", "ok": None, "detail": "当前执行器尚未配置该项目语言的编译与运行环境"}
    if not docker_available():
        return {"label": "隔离安装与运行测试", "ok": None, "detail": "执行沙箱不可用；未执行依赖安装、构建与功能测试"}
    runtime = "node" if runtime == "node" else "python"
    if runtime == "node":
        image = os.getenv("OFFICE_NODE_IMAGE", "node:22-bookworm-slim")
        install = "npm install --ignore-scripts --no-audit --no-fund"
        test = "node -e \"const p=require('./package.json'); if(!p.scripts?.test)process.exit(3)\" && npm run build --if-present && npm test"
    else:
        image = os.getenv("OFFICE_PYTHON_IMAGE", "python:3.12-slim")
        install = "if [ -f requirements.txt ]; then python -m pip install --only-binary=:all: --disable-pip-version-check --target /work/.python-deps -r requirements.txt; fi"
        test = "if python -c 'import pytest' 2>/dev/null; then python -m pytest -q; elif [ -d tests ]; then python -m unittest discover -s tests -v; else python -m unittest discover -v; fi"
    name = "office-check-" + uuid.uuid4().hex[:12]
    volume = name + "-files"
    executable = shutil.which("docker")
    common = [executable, "run", "--rm", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128",
              "--memory=1536m", "--cpus=2", "--read-only", "--tmpfs=/tmp:rw,exec,size=384m,mode=1777",
              "--mount", f"type=volume,source={volume},target=/work", "--env", "HOME=/tmp",
              "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=/work/.python-deps:/work"]
    stages = [common + ["--name", name + "-install", "--mount", f"type=bind,source={root.resolve()},target=/src,readonly",
                        image, "/bin/sh", "-lc", "cp -R /src/. /work/ && cd /work && " + install + " && chmod -R a+rwX /work"],
              common + ["--name", name + "-test", "--network=none", "--user=65534:65534", image, "/bin/sh", "-lc", "cd /work && " + test]]
    try:
        output = ""
        for stage, command in zip(("依赖安装", "构建与功能测试"), stages):
            with tempfile.TemporaryFile() as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=300,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                log.seek(0, 2)
                size = log.tell()
                log.seek(max(0, size - 16000))
                output += f"\n[{stage}]\n" + log.read().decode("utf-8", errors="replace")
            if result.returncode:
                return {"label": "隔离安装与运行测试", "ok": False, "detail": output[-14000:], "runtime": runtime}
        empty = bool(re.search(r"Ran 0 tests|no tests ran|No tests found", output, re.I))
        evidence = bool(re.search(r"Ran\s+[1-9]\d*\s+tests?|\b[1-9]\d*\s+(?:passed|passing)|\btests?\s+[1-9]\d*", output, re.I))
        return {"label": "隔离安装与运行测试", "ok": not empty and evidence,
                "detail": output[-14000:] + ("\n未找到非零测试数量的执行证据。" if not evidence else ""), "runtime": runtime}
    except subprocess.TimeoutExpired:
        return {"label": "隔离安装与运行测试", "ok": False, "detail": "依赖安装、构建或测试超过 300 秒"}
    except OSError as error:
        return {"label": "隔离安装与运行测试", "ok": None, "detail": str(error)}
    finally:
        for suffix in ("-install", "-test"):
            subprocess.run([executable, "rm", "-f", name + suffix], capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.run([executable, "volume", "rm", "-f", volume], capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def evaluate(root: Path, planned: list[str], runtime: str, execute=True) -> dict:
    checks = static_check(root, planned)
    if not any(c["ok"] is False for c in checks) and execute:
        checks.append(sandbox_check(root, runtime))
    else:
        checks.append({"label": "隔离安装与运行测试", "ok": None, "detail": "静态检查未通过或本次未执行"})
    status = "failed" if any(c["ok"] is False for c in checks) else "unchecked" if any(c["ok"] is None for c in checks) else "passed"
    return {"schema": 1, "kind": "code", "status": status, "ok": status == "passed", "checks": checks,
            "pages": [], "metrics": {"files": len(list(project_files(root)))}, "review": {"status": "not_run"}}


def ensure_python_dependency_manifest(root: Path, planned: list[str], runtime: str) -> bool:
    """Create a dependency manifest when all discovered third-party imports are known."""
    relative = "requirements.txt"
    target = safe_path(root, relative)
    if runtime != "python" or relative not in planned or target.exists():
        return False
    local_names = {path.stem for path in project_files(root) if path.suffix == ".py"}
    local_names.update(path.name for path in root.iterdir() if path.is_dir())
    imports = set()
    for path in project_files(root):
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imports.add(node.module.split(".", 1)[0])
    external = imports - set(getattr(sys, "stdlib_module_names", ())) - local_names
    distributions = {"pytest": "pytest"}
    if external - set(distributions):
        return False
    lines = (["# No third-party dependencies required; the project uses Python's standard library."]
             if not external else [distributions[module] for module in sorted(external)])
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def assemble(requirement: str, contributions: Path, destination: Path, roster: int | None = None) -> tuple[Path, dict]:
    destination.mkdir(parents=True, exist_ok=True)
    merged, conflicts = merge_contributions(contributions, destination)
    if conflicts:
        return destination, {"ok": False, "status": "failed", "kind": "code", "merged_contributions": merged,
                             "checks": [{"label": "贡献文件冲突", "ok": False, "detail": str(conflicts)}], "pages": []}
    evidence = snapshot(contributions)
    plan = chat_json(
        "你是交付负责人。将需求与团队已有代码整合为完整可运行项目。先规划架构与共享接口。"
        '返回 JSON {"runtime":"python|node|unsupported","files":[{"path":"相对路径","purpose":"具体职责"}]}。'
        "尊重用户指定语言与框架；当前执行器未支持的语言标记为 unsupported，不得擅自换语言。"
        "根据任务使用合适栈，必须规划 README.md、依赖清单、入口和真正断言功能的测试。"
        "测试必须真实执行成功与失败路径；禁止 pytest.skip/xfail、unittest.skip 或其他跳过方式。"
        "前后端接口要统一，不能把每个成员的示例脚本拼在一起。最多40个文件。",
        f"需求：{requirement}\n已有成果：\n{evidence}", fallback={}, roster=roster, max_tokens=3500, timeout=180,
    )
    if not isinstance(plan, dict) or not isinstance(plan.get("files"), list):
        return destination, {"ok": False, "status": "failed", "kind": "code", "checks": [{"label": "项目规划", "ok": False, "detail": "模型未返回可用项目计划"}], "pages": []}
    planned = []
    for item in plan["files"][:MAX_FILES]:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            try:
                safe_path(destination, item["path"])
                planned.append(item["path"].replace("\\", "/"))
            except ValueError:
                pass
    planned = list(dict.fromkeys(planned))
    for offset in range(0, len(planned), 4):
        batch = planned[offset:offset + 4]
        raw = chat_text(
            "你是资深全栈工程师，输出完整项目文件，使用 ===FILE: 路径\\n完整内容\\n===END 格式。"
            "所有路径和接口应与项目规划一致。必须完成真实功能、错误处理、输入校验和测试断言。"
            "禁止 TODO、未实现的空函数和伪造已测试结论。配置中的密钥用环境变量，不写入真实凭据。"
            "只写项目实际需要的代码与 README，不把对用户的解释、生成说明、示例免责声明或 AI 水印写入文件。",
            [{"role": "user", "content": json.dumps({"requirement": requirement, "plan": plan,
                "write_only": batch, "existing_project": snapshot(destination), "team_contributions": evidence[:14000]}, ensure_ascii=False)}],
            fallback="", roster=roster, max_tokens=14000, timeout=300, temperature=.35,
        )
        existing = {item["path"] for item in merged}
        write_response(destination, raw, set(batch) - existing)
    runtime = plan.get("runtime", "python")
    ensure_python_dependency_manifest(destination, planned, runtime)
    report = evaluate(destination, planned, runtime)
    rounds = [{"attempt": 1, "status": report["status"]}]
    for attempt in range(2):
        failures = [c for c in report["checks"] if c["ok"] is False]
        if not failures:
            break
        # 修复在新目录进行；较差的候选不会覆盖上一版项目。
        candidate = destination.parent / f"{destination.name} (修订{attempt + 1})"
        shutil.copytree(destination, candidate)
        raw = chat_text(
            "修复实际检查发现的问题，仅输出需要替换或新增的完整文件，使用 ===FILE: 路径\\n内容\\n===END。"
            "不能删除测试、跳过测试、把断言改成恒真或移除用户功能来让检查通过。"
            "README 的启动和测试命令必须从正确工作目录运行；requirements.txt 有依赖时明确给出 python -m pip install -r requirements.txt。"
            "不要在代码或 README 中加入生成说明、示例免责声明或 AI 水印。",
            [{"role": "user", "content": json.dumps({"requirement": requirement, "errors": failures, "files": snapshot(destination)}, ensure_ascii=False)}],
            fallback="", roster=roster, max_tokens=14000, timeout=300, temperature=.25,
        )
        write_response(candidate, raw)
        repaired = evaluate(candidate, planned, runtime)
        rounds.append({"attempt": attempt + 2, "status": repaired["status"]})
        if sum(c["ok"] is False for c in repaired["checks"]) <= len(failures):
            destination, report = candidate, repaired
    report.update(attempts=rounds, requirement=requirement, runtime=runtime, planned_files=planned,
                  merged_contributions=merged)
    return destination, report
