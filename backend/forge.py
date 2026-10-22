"""真实交付物锻造间：LLM 直接编写 Python 生成脚本，后端执行产出真实文件

安全说明：本模块会在本机执行 LLM 生成的 Python 脚本（subprocess，cwd 限定在
deliverables 目录，120 秒超时）。这是用户明确选择的生成策略，请勿在不受信的
多租户环境开启。任何失败都会返回 None，由调用方回退到 Markdown 兜底产物。
"""

import html as html_lib
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from llm import chat_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DELIVERABLES_DIR = os.path.join(BASE_DIR, "deliverables")
CODE_ROOT = os.path.join(DELIVERABLES_DIR, "code")
# 当前 Sprint 的代码目录（每轮独立，避免新任务覆盖历史代码）
CODE_DIR = CODE_ROOT

os.makedirs(DELIVERABLES_DIR, exist_ok=True)
os.makedirs(CODE_DIR, exist_ok=True)

MAX_REPAIR = 2  # 脚本执行失败后回喂报错让 LLM 修复的轮数
SCRIPT_TIMEOUT = 120

_KIND_META = {
    "docx": {"lib": "python-docx（import docx）", "desc": "Word 文档"},
    "pptx": {"lib": "python-pptx（import pptx）", "desc": "PPT 演示文稿"},
    "xlsx": {"lib": "openpyxl（import openpyxl）", "desc": "Excel 表格"},
}


def _unique_name(filename: str) -> str:
    """避免同名覆盖：已存在时在文件名后追加时间戳"""
    path = os.path.join(DELIVERABLES_DIR, filename)
    if not os.path.exists(path):
        return filename
    stem, dot, ext = filename.rpartition(".")
    return f"{stem}_{int(time.time())}.{ext}" if dot else f"{filename}_{int(time.time())}"


# ---------- 目标位置：从需求文本解析「放在/保存到 某某文件夹」 ----------

_TARGET_PATTERN = re.compile(
    r"(?:放在|保存到|存到|放到|生成到|输出到|存入)\s*[「『\"']?([^\s，。,.;；!！?？「」『』\"']+)"
)
# 兜底：「生成空文件在桌面上」这类「在+常见目录」的口语表达
_FALLBACK_PATTERN = re.compile(
    r"在\s*(桌面|下载|文档|图片)(?:[的上里下]\s*([^\s，。,.;；!！?？「」『』\"']+))?"
)
# 常见口语目录名 → 系统真实 Known Folder（兼容 OneDrive 重定向，如桌面被重定向到 OneDrive\桌面）
_SPECIAL_DIRS = {
    "桌面": "Desktop",
    "下载": "Downloads",
    "文档": "Documents",
    "图片": "Pictures",
    "音乐": "Music",
    "视频": "Videos",
}

# 用户在界面显式选择的保存位置：优先级最高，覆盖文本解析结果
EXPLICIT_SAVE_DIR = ""


def set_explicit_save_dir(path: str) -> None:
    global EXPLICIT_SAVE_DIR
    EXPLICIT_SAVE_DIR = (path or "").strip()


def list_known_locations() -> list[dict]:
    """供前端「保存位置」下拉展示的真实系统目录（兼容 OneDrive 重定向）"""
    items = []
    for label, folder in (
        ("桌面", "Desktop"),
        ("文档", "Documents"),
        ("下载", "Downloads"),
        ("图片", "Pictures"),
        ("音乐", "Music"),
        ("视频", "Videos"),
    ):
        try:
            path = _known_folder(folder)
        except Exception:  # noqa: BLE001 —— 单个目录解析失败不影响列表
            path = os.path.join(os.path.expanduser("~"), folder)
        items.append({"name": label, "path": path})
    return items

# Windows Known Folder GUID（Vista+ 标准目录）
_KNOWN_FOLDER_IDS = {
    "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "Pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "Music": "{2112AB0A-C86A-4FFE-A368-0DE96E47012E}",
    "Videos": "{35286A68-3C57-41A1-BBB1-0EAE73D76C95}",
}

# 明显不是路径的短语（避免把「放在第一页」当成文件夹）
_NON_PATH_ENDINGS = ("页", "章", "段", "行", "句", "条", "次", "天", "小时", "分钟", "个", "份", "张", "篇")
_NON_PATH_WORDS = {"这里", "那里", "里面", "外面", "上面", "下面", "中间", "旁边", "最后", "开头"}


def _known_folder(name: str) -> str:
    """取系统真实目录；Windows 下用 Known Folder API，失败回退 ~/name"""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            value = uuid.UUID(_KNOWN_FOLDER_IDS[name])
            guid = GUID(
                value.fields[0],
                value.fields[1],
                value.fields[2],
                (ctypes.c_ubyte * 8)(*value.bytes[8:]),
            )
            path = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            shell32.SHGetKnownFolderPath.argtypes = [
                ctypes.POINTER(GUID),
                wintypes.DWORD,
                wintypes.HANDLE,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            shell32.SHGetKnownFolderPath.restype = ctypes.c_long
            if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) == 0:
                resolved = path.value
                ctypes.windll.ole32.CoTaskMemFree(path)
                if resolved and os.path.isdir(resolved):
                    return resolved
        except Exception as error:  # noqa: BLE001 —— 取不到就用家目录兜底
            print(f"[Forge] Known Folder 解析失败 {name}：{error}")
    return os.path.join(os.path.expanduser("~"), name)


def _looks_non_path(text: str) -> bool:
    return text in _NON_PATH_WORDS or (
        not re.search(r"[\\/]", text) and text.endswith(_NON_PATH_ENDINGS)
    )


def _cut_clause(text: str) -> str:
    """截断口语里的并列/后续动作，避免把「桌面上并生成一份报告」整段当目录名"""
    return re.split(r"(?:并|然后|再|同时|之后|以后|，|,|。|；|;)", text)[0].strip()


def _special_dir(name: str) -> str:
    return _known_folder(_SPECIAL_DIRS[name])


def _strip_dir_suffix(text: str) -> str:
    """剥掉尾部「文件夹/下/里/面」等语气后缀（保证剥完还有内容）"""
    for suffix in ("文件夹下", "文件夹里", "文件夹", "目录下", "目录里", "下面", "里面", "下", "里", "面", "上", "中"):
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)].strip()
    return text


def extract_target_dir(text: str) -> str | None:
    """解析需求里的目标保存位置：界面显式选择优先；绝对路径原样使用；
    桌面/下载/文档自动映射；其他短名称视为「文档」下的子文件夹。解析不到返回 None。"""
    if EXPLICIT_SAVE_DIR:
        return EXPLICIT_SAVE_DIR
    text = text or ""
    # 「桌面的 AI产物」这类中英混排常在「的」后带空格，先合并，避免只解析到「桌面的」
    if not re.search(r"[A-Za-z]:[\\/]", text):
        text = re.sub(r"(?<=[的上下里中面])\s+(?=[\w\u4e00-\u9fff])", "", text)
    match = _TARGET_PATTERN.search(text)
    if not match:
        # 兜底：「…在桌面上 / 在桌面的XX文件夹」
        fallback_match = _FALLBACK_PATTERN.search(text)
        if not fallback_match:
            return None
        base = _special_dir(fallback_match.group(1))
        sub = _strip_dir_suffix((fallback_match.group(2) or "").strip())
        if sub and _looks_non_path(sub):
            sub = ""
        return os.path.join(base, sub) if sub else base
    raw = match.group(1).strip()
    if not raw:
        return None
    # 非绝对路径先截断并列动作，再判断是否是「路径类」表达
    is_absolute = bool(re.match(r"^[A-Za-z]:[\\/]", raw)) or raw.startswith(("\\", "/", "~"))
    if not is_absolute:
        raw = _cut_clause(raw)
        if not raw:
            return None
    # 明显不是路径的表达（如「放在第一页」「放在最后」）直接忽略
    if _looks_non_path(raw):
        return None
    # 绝对路径：只剥掉「下/里/面」方位后缀，保留路径中的「文件夹」字样
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("\\", "/", "~")):
        for suffix in ("目录下", "目录里", "下面", "里面", "下", "里", "面"):
            if raw.endswith(suffix) and len(raw) > len(suffix) + 2:
                raw = raw[: -len(suffix)].strip()
                break
        return os.path.expandvars(os.path.expanduser(raw))
    # 特殊口语目录优先整词匹配
    if raw in _SPECIAL_DIRS:
        return _special_dir(raw)
    # 「桌面的XX文件夹」「桌面上XX」等结构：特殊目录 + 子文件夹
    prefixed = re.match(r"^(桌面|下载|文档|图片)[的上里下](.+)$", raw)
    if prefixed and prefixed.group(2):
        sub = prefixed.group(2).strip()
        for suffix in ("文件夹下", "文件夹里", "文件夹", "下面", "里面", "下", "里", "面", "上", "中"):
            if sub.endswith(suffix) and len(sub) > len(suffix):
                sub = sub[: -len(suffix)].strip()
                break
        base = _special_dir(prefixed.group(1))
        return os.path.join(base, sub) if sub else base
    # 再剥掉尾部「文件夹/下/里/面」等语气后缀（保证剥完还有内容）
    for suffix in ("文件夹下", "文件夹里", "文件夹", "目录下", "目录里", "下面", "里面", "下", "里", "面", "上", "中"):
        if raw.endswith(suffix) and len(raw) > len(suffix):
            raw = raw[: -len(suffix)].strip()
            break
    if not raw:
        return None
    if raw in _SPECIAL_DIRS:
        return _special_dir(raw)
    # 短名称：建在「文档」下的子文件夹
    return os.path.join(_special_dir("文档"), raw)


def copy_to_target(file_name: str, target_dir: str) -> str | None:
    """把 deliverables 里的成品复制到用户指定目录，返回目标完整路径"""
    src = safe_deliverable_path(file_name)
    if not src or not target_dir:
        return None
    try:
        os.makedirs(target_dir, exist_ok=True)
        dest = os.path.join(target_dir, os.path.basename(src))
        shutil.copy2(src, dest)
        return dest
    except Exception as error:  # noqa: BLE001 —— 复制失败不影响主流程
        print(f"[Forge] 复制到目标位置失败 {target_dir}：{error}")
        return None


def duplicate_deliverable(file_name: str) -> str | None:
    """复制一个已有成品文件（自动取唯一名），返回新文件名；源文件不存在返回 None

    用于版本回滚：旧版文件必须复制为新文件，不能让两条产物记录共享同一个
    file_name，否则删除其中一条会把另一条的文件一起删掉。"""
    src = safe_deliverable_path(file_name)
    if not src:
        return None
    new_name = _unique_name(file_name)
    try:
        shutil.copy2(src, os.path.join(DELIVERABLES_DIR, new_name))
        return new_name
    except Exception as error:  # noqa: BLE001
        print(f"[Forge] 复制版本文件失败 {file_name}：{error}")
        return None


def extract_code(llm_text: str) -> str:
    """从 LLM 回复中提取 python 代码块；没有代码块时把全文当代码"""
    if not llm_text:
        return ""
    match = re.search(r"```(?:python)?\s*\n(.*?)```", llm_text, re.DOTALL)
    return (match.group(1) if match else llm_text).strip()


# 脚本安全防护：禁止联网/系统调用/越界文件操作（启发式，非完整沙箱）
_BLOCKED_SCRIPT_PATTERNS = (
    (re.compile(r"\b(subprocess|socket|requests|urllib|httpx|http\.client|ftplib|smtplib|telnetlib)\b"), "禁止联网或调用外部进程"),
    (re.compile(r"\b(ctypes|winreg|win32api|win32con|msvcrt)\b"), "禁止调用系统底层接口"),
    (re.compile(r"\bos\.(system|popen|remove|unlink|rmdir|rename|replace)\b"), "禁止操作系统文件"),
    (re.compile(r"\bshutil\.(rmtree|move|copy|copyfile)\b"), "禁止批量操作文件"),
    (re.compile(r"\b(eval|exec|compile|__import__)\s*\("), "禁止动态执行代码"),
    (re.compile(r"\bopen\s*\("), "禁止直接读写文件"),
)


def check_script_safety(code: str) -> str:
    """返回第一处违规说明；空字符串表示通过"""
    for pattern, reason in _BLOCKED_SCRIPT_PATTERNS:
        if pattern.search(code):
            return reason
    return ""


def _safe_env() -> dict:
    """只保留运行 Python 所需的最小环境变量，避免 API Key 等泄漏给生成脚本"""
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PYTHONIOENCODING"}
    return {k: v for k, v in os.environ.items() if k.upper() in keep}


def run_script(code: str) -> tuple[bool, str]:
    """在 deliverables 目录下执行脚本，返回 (是否成功, 错误输出)

    注意：这是启发式防护，不是安全沙箱；项目仅建议在本机自用场景运行。"""
    violation = check_script_safety(code)
    if violation:
        return False, f"脚本包含不安全操作：{violation}"
    script_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8",
            dir=DELIVERABLES_DIR,
        ) as f:
            f.write(code)
            script_path = f.name
        proc = subprocess.run(
            [sys.executable, "-I", script_path],
            cwd=DELIVERABLES_DIR,
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
            encoding="utf-8",
            errors="replace",
            env=_safe_env(),
        )
        if proc.returncode == 0:
            return True, ""
        return False, (proc.stderr or proc.stdout or "未知错误")[-1500:]
    except subprocess.TimeoutExpired:
        return False, f"脚本执行超过 {SCRIPT_TIMEOUT} 秒被终止"
    except Exception as error:  # noqa: BLE001
        return False, f"执行器异常：{error}"
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except OSError:
                pass


def _script_prompt(kind: str, spec: str, output_path: str) -> str:
    meta = _KIND_META[kind]
    return (
        f"你是资深办公自动化工程师。写一个完整、可直接运行的 Python 脚本，"
        f"使用 {meta['lib']} 生成一份高质量的{meta['desc']}。\n"
        f"硬性要求：\n"
        f"1. 内容必须详实专业、贴近真实工作产出（质量优先，不要占位符式的空洞内容）；\n"
        f"2. 所有文字使用中文，字体设置为「微软雅黑」或「宋体」；\n"
        f"3. 脚本最后必须把成品保存到这个绝对路径：{output_path}\n"
        f"4. 只输出一个 ```python 代码块，不要任何解释文字；\n"
        f"5. 不得写 deliverables 目录以外的任何文件，不得访问网络；"
        f"允许只读访问 uploads 目录下的用户附件；\n"
        f"6. 脚本要紧凑高效：用循环/列表批量添加内容，避免逐行重复调用，总长度控制在 150 行以内。\n"
        f"需求内容：{spec}"
    )


def forge_file(kind: str, spec: str, filename: str, roster: int | None = None) -> str | None:
    """让 LLM 写脚本并执行，生成真实 docx/pptx/xlsx；成功返回文件名，失败返回 None"""
    if kind not in _KIND_META:
        return None
    filename = _unique_name(filename)
    output_path = os.path.join(DELIVERABLES_DIR, filename)

    code = extract_code(chat_text(
        "你只输出可运行的 Python 脚本，用代码块包裹。",
        [{"role": "user", "content": _script_prompt(kind, spec, output_path)}],
        fallback="",
        temperature=0.4,
        max_tokens=8000,
        timeout=300,
        roster=roster,
    ))
    if not code:
        return None

    for attempt in range(1 + MAX_REPAIR):
        ok, err = run_script(code)
        if ok and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return filename
        print(f"[Forge] 第 {attempt + 1} 轮失败（{filename}）：{err[:300]}")
        if not ok and "ModuleNotFoundError" in err:
            break  # 环境缺依赖，重试无意义
        if attempt >= MAX_REPAIR:
            break
        repaired = chat_text(
            "你只输出修复后的完整 Python 脚本，用代码块包裹。",
            [
                {"role": "user", "content": _script_prompt(kind, spec, output_path)},
                {"role": "assistant", "content": f"```python\n{code}\n```"},
                {"role": "user", "content": (
                    f"脚本执行失败，错误信息：\n{err}\n"
                    "请修复后输出完整脚本（仍须保存到同一路径）。"
                    "注意控制脚本总长度，避免输出被截断导致字符串不闭合；"
                    "长文本内容可以拆分多次调用，不要在单行里写过长的字符串。"
                )},
            ],
            fallback="",
            temperature=0.3,
            max_tokens=8000,
            timeout=300,
            roster=roster,
        )
        code = extract_code(repaired)
        if not code:
            break
    print(f"[Forge] {kind} 生成失败，回退 Markdown：{filename}")
    return None


def forge_code(task_title: str, requirement: str, subdir: str = "", roster: int | None = None) -> list[str]:
    """让 LLM 为开发任务直接产出真实代码文件，落盘到 deliverables/code/；返回相对路径列表"""
    target_dir = os.path.join(CODE_DIR, subdir) if subdir else CODE_DIR
    os.makedirs(target_dir, exist_ok=True)

    raw = chat_text(
        "你是资深全栈工程师。根据开发任务产出真实、可运行的代码。"
        "只输出一个 JSON 对象（不要用代码块包裹）："
        '{"files":[{"path":"相对路径/文件名","content":"完整文件内容"}]}'
        "要求：代码完整可运行、带必要注释、贴近工程实践；文件数量 1-4 个；"
        "path 只能是相对路径，不允许 .. 或绝对路径。",
        [{"role": "user", "content": f"所属需求：{requirement}\n开发任务：{task_title}"}],
        fallback="",
        temperature=0.4,
        max_tokens=6000,
        timeout=300,
        roster=roster,
    )
    if not raw:
        return []

    try:
        payload = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
        files = payload.get("files", [])
    except (json.JSONDecodeError, AttributeError):
        print(f"[Forge] 代码 JSON 解析失败：{task_title}")
        return []

    written: list[str] = []
    for item in files[:4]:
        rel = str(item.get("path", "")).strip().lstrip("/\\").replace("\\", "/")
        content = str(item.get("content", ""))
        if not rel or ".." in rel.split("/") or not content:
            continue
        abs_path = os.path.realpath(os.path.join(target_dir, rel))
        if not abs_path.startswith(os.path.realpath(target_dir)):
            continue
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            written.append(os.path.relpath(abs_path, DELIVERABLES_DIR).replace("\\", "/"))
        except OSError as error:
            print(f"[Forge] 代码落盘失败 {rel}：{error}")
    return written


def begin_sprint(sprint_id: str) -> str:
    """切换当前 Sprint 的代码目录：每轮独立存放，历史代码不会被新任务删除"""
    global CODE_DIR
    safe_id = re.sub(r"[^\w-]", "", sprint_id or "") or "current"
    CODE_DIR = os.path.join(CODE_ROOT, safe_id)
    os.makedirs(CODE_DIR, exist_ok=True)
    return CODE_DIR


def sprint_code_dir(sprint_id: str) -> str:
    safe_id = re.sub(r"[^\w-]", "", sprint_id or "") or "current"
    return os.path.join(CODE_ROOT, safe_id)


def clear_code_dir() -> None:
    """兼容旧调用：只清理当前目录内的文件，不删除历史 Sprint"""
    if os.path.isdir(CODE_DIR):
        for name in os.listdir(CODE_DIR):
            path = os.path.join(CODE_DIR, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    os.makedirs(CODE_DIR, exist_ok=True)


def verify_generated_code() -> list[str]:
    """对产出代码做基础可运行性检查：Python 语法编译 + Node 语法检查"""
    failures: list[str] = []
    for py_file in Path(CODE_DIR).rglob("*.py"):
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
        for js_file in list(Path(CODE_DIR).rglob("*.js")) + list(Path(CODE_DIR).rglob("*.mjs")):
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


def finalize_project(requirement: str) -> list[str]:
    """为代码类交付补齐 README 与 requirements，返回新增文件名列表"""
    files = [p for p in Path(CODE_DIR).rglob("*") if p.is_file()]
    names = [str(p.relative_to(CODE_DIR)).replace("\\", "/") for p in files]
    added: list[str] = []

    # requirements.txt：从 import 语句推断常见第三方依赖
    packages = set()
    for py_file in Path(CODE_DIR).rglob("*.py"):
        try:
            text = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for module, package in {
            "fastapi": "fastapi",
            "uvicorn": "uvicorn[standard]",
            "flask": "flask",
            "requests": "requests",
            "httpx": "httpx",
            "pandas": "pandas",
            "numpy": "numpy",
            "openpyxl": "openpyxl",
            "docx": "python-docx",
            "pptx": "python-pptx",
            "pydantic": "pydantic",
            "sqlalchemy": "sqlalchemy",
            "dotenv": "python-dotenv",
        }.items():
            if re.search(rf"^\s*(import|from)\s+{module}\b", text, re.MULTILINE):
                packages.add(package)
    requirements = Path(CODE_DIR) / "requirements.txt"
    requirements.write_text("\n".join(sorted(packages)) + "\n", encoding="utf-8")
    added.append("requirements.txt")

    main_file = next((n for n in names if n.endswith(".py") and n.split("/")[-1] == "main.py"), "")
    run_hint = f"python {main_file}" if main_file else "python <入口文件>.py"
    readme = (
        f"# {requirement[:40] or 'AI 生成项目'}\n\n"
        "> 由 AI 敏捷开发办公室自动生成。\n\n"
        "## 文件结构\n\n"
        + ("\n".join(f"- `{n}`" for n in sorted(names)) or "- （暂无文件）")
        + "\n\n## 运行方式\n\n```bash\n"
        "python -m venv .venv\n"
        "pip install -r requirements.txt\n"
        f"{run_hint}\n```\n\n"
        "## 说明\n\n"
        "- 代码已通过 Python 语法编译检查（若为 JS 项目则通过 Node 语法检查）。\n"
        "- 运行前请确认已安装 requirements.txt 中的依赖。\n"
        "- 生成代码仅供本地学习与演示，请勿直接用于生产环境。\n"
    )
    (Path(CODE_DIR) / "README.md").write_text(readme, encoding="utf-8")
    added.append("README.md")
    return added


def render_docx_from_markdown(content: str, filename: str) -> str | None:
    """确定性渲染：把 Markdown 文本直接渲染为真实 docx（不调用 LLM）

    支持 # / ## / ### 标题、"- " 无序列表，其余按普通段落。
    用于演示模式与产物站内文本编辑（编辑后确定性重渲染为新版本文件）。"""
    filename = _unique_name(filename)
    output_path = os.path.join(DELIVERABLES_DIR, filename)
    try:
        from docx import Document

        doc = Document()
        for raw in (content or "").splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("### "):
                doc.add_heading(stripped[4:].strip(), level=3)
            elif stripped.startswith("## "):
                doc.add_heading(stripped[3:].strip(), level=2)
            elif stripped.startswith("# "):
                doc.add_heading(stripped[2:].strip(), level=1)
            elif stripped.startswith("- "):
                doc.add_paragraph(stripped[2:].strip(), style="List Bullet")
            else:
                doc.add_paragraph(stripped)
        doc.save(output_path)
        return filename if os.path.getsize(output_path) > 0 else None
    except Exception as error:  # noqa: BLE001
        print(f"[Forge] Markdown 渲染 docx 失败：{error}")
        return None


def safe_deliverable_path(file_name: str) -> str | None:
    """下载接口用：校验文件名必须落在 deliverables 目录内，防路径穿越"""
    if not file_name:
        return None
    abs_path = os.path.realpath(os.path.join(DELIVERABLES_DIR, file_name))
    if not abs_path.startswith(os.path.realpath(DELIVERABLES_DIR)):
        return None
    return abs_path if os.path.isfile(abs_path) else None


def extract_preview(file_name: str, limit: int = 1500) -> str:
    """从生成的真实文件中提取文本，作为产物中心的正文预览"""
    path = safe_deliverable_path(file_name)
    if not path:
        return ""
    try:
        if file_name.endswith(".docx"):
            from docx import Document

            paras = [
                p.text.strip()
                for p in Document(path).paragraphs
                if p.text.strip()
            ]
            for table in Document(path).tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        paras.append(" | ".join(cells))
            return "\n".join(paras)[:limit]
        if file_name.endswith(".pptx"):
            from pptx import Presentation

            lines: list[str] = []
            for i, slide in enumerate(Presentation(path).slides, 1):
                lines.append(f"【第 {i} 页】")
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            text = "".join(run.text for run in para.runs).strip()
                            if text:
                                lines.append(text)
            return "\n".join(lines)[:limit]
        if file_name.endswith(".xlsx"):
            from openpyxl import load_workbook

            wb = load_workbook(path, read_only=True, data_only=True)
            lines = []
            for ws in wb.worksheets:
                lines.append(f"【工作表：{ws.title}】")
                for row in ws.iter_rows(max_row=10, max_col=6, values_only=True):
                    cells = [str(c) for c in row if c is not None]
                    if cells:
                        lines.append(" | ".join(cells))
                lines.append("")
            return "\n".join(lines)[:limit]
    except Exception as error:  # noqa: BLE001 —— 预览失败不影响文件本身
        print(f"[Forge] 预览提取失败 {file_name}：{error}")
    return ""


def preview_html(file_name: str, max_rows: int = 60) -> str:
    """生成结构化 HTML 预览（Word 标题/段落/表格、PPT 分页、Excel 工作表），
    所有文本均转义，前端可直接渲染。"""
    path = safe_deliverable_path(file_name)
    if not path:
        return ""
    esc = html_lib.escape
    try:
        if file_name.endswith(".docx"):
            from docx import Document

            doc = Document(path)
            parts: list[str] = []
            for p in doc.paragraphs:
                text = p.text.strip()
                if not text:
                    continue
                style = (p.style.name or "").lower()
                tag = (
                    "h1" if "heading 1" in style or style == "title"
                    else "h2" if "heading 2" in style
                    else "h3" if "heading 3" in style
                    else "p"
                )
                parts.append(f"<{tag}>{esc(text)}</{tag}>")
            for table in doc.tables:
                rows = []
                for row in table.rows[:max_rows]:
                    cells = "".join(f"<td>{esc(c.text.strip())}</td>" for c in row.cells)
                    rows.append(f"<tr>{cells}</tr>")
                if rows:
                    parts.append(f"<table>{''.join(rows)}</table>")
            return "".join(parts)
        if file_name.endswith(".pptx"):
            from pptx import Presentation

            parts = []
            for index, slide in enumerate(Presentation(path).slides, 1):
                texts = []
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            text = "".join(run.text for run in para.runs).strip()
                            if text:
                                texts.append(f"<p>{esc(text)}</p>")
                parts.append(
                    f'<section class="preview-slide"><h3>第 {index} 页</h3>{"".join(texts)}</section>'
                )
            return "".join(parts)
        if file_name.endswith(".xlsx"):
            from openpyxl import load_workbook

            wb = load_workbook(path, read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets:
                rows = []
                for row in ws.iter_rows(max_row=max_rows, max_col=8, values_only=True):
                    cells = "".join(
                        f"<td>{esc('' if c is None else str(c))}</td>" for c in row
                    )
                    rows.append(f"<tr>{cells}</tr>")
                parts.append(
                    f'<section class="preview-sheet"><h3>{esc(ws.title)}</h3>'
                    f'<table>{"".join(rows)}</table></section>'
                )
            return "".join(parts)
    except Exception as error:  # noqa: BLE001
        print(f"[Forge] HTML 预览失败 {file_name}：{error}")
    return ""


def self_check(file_name: str, kind: str | None = None) -> dict:
    """成品自检：检查真实文件是否具备可交付的基本要素（章节/页数/行列/体积）"""
    path = safe_deliverable_path(file_name)
    checks: list[dict] = []
    if not path:
        return {
            "ok": False,
            "checks": [{"label": "文件存在", "ok": False, "detail": "未找到生成的文件"}],
        }
    ext = (kind or file_name.rpartition(".")[2]).lower()
    try:
        if ext == "docx":
            from docx import Document

            doc = Document(path)
            paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            chars = sum(len(p) for p in paras)
            checks.append(
                {"label": "正文章节", "ok": len(paras) >= 5, "detail": f"{len(paras)} 个段落"}
            )
            checks.append(
                {"label": "正文篇幅", "ok": chars >= 300, "detail": f"{chars} 字"}
            )
        elif ext == "pptx":
            from pptx import Presentation

            slides = list(Presentation(path).slides)
            text_chars = 0
            for slide in slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        text_chars += len(shape.text_frame.text.strip())
            checks.append(
                {"label": "幻灯片页数", "ok": len(slides) >= 3, "detail": f"{len(slides)} 页"}
            )
            checks.append(
                {"label": "页面内容", "ok": text_chars >= 80, "detail": f"{text_chars} 字"}
            )
        elif ext == "xlsx":
            from openpyxl import load_workbook

            wb = load_workbook(path, read_only=True, data_only=True)
            rows = sum(ws.max_row or 0 for ws in wb.worksheets)
            checks.append(
                {"label": "工作表", "ok": len(wb.worksheets) >= 1, "detail": f"{len(wb.worksheets)} 个"}
            )
            checks.append(
                {"label": "数据行数", "ok": rows >= 3, "detail": f"{rows} 行"}
            )
        else:
            checks.append(
                {"label": "文件体积", "ok": os.path.getsize(path) > 0, "detail": f"{os.path.getsize(path)} 字节"}
            )
    except Exception as error:  # noqa: BLE001 —— 自检失败按未通过处理，如实告知
        checks.append({"label": "解析文件", "ok": False, "detail": str(error)[:120]})
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


def self_check_note(result: dict) -> str:
    """把自检结果格式化为可直接附在产物正文后的 Markdown 小节"""
    lines = ["", "", "## 成品自检", ""]
    for item in result.get("checks", []):
        mark = "✅" if item["ok"] else "⚠️"
        lines.append(f"- {mark} {item['label']}：{item['detail']}")
    if not result.get("ok"):
        lines.append("")
        lines.append("> 自检未全部通过，成品可能不完整，建议点击「重新生成」再试一次。")
    return "\n".join(lines)
