"""产物路径与保存位置：文件标识解析、路径穿越防护、归档与目标目录解析。

产物目录（WORKSPACE_DIR / DELIVERABLES_DIR）由 forge 持有，运行时可被覆盖
（测试与部署会改 forge 的这两个属性）。这里读取时以 forge 的当前值为准，
避免两套常量各说各话导致文件写错位置。
"""

import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from artifact_naming import semantic_stem

_PUBLISH_LOCK = threading.Lock()


def _workspace_dir() -> str:
    import forge
    return forge.WORKSPACE_DIR


def _deliverables_dir() -> str:
    import forge
    return forge.DELIVERABLES_DIR


def _unique_name(filename: str) -> str:
    """过程文件和下载缓存放在 workspace；UUID 避免并行生成覆盖。"""
    filename = os.path.basename(filename.replace("\\", "/"))
    name = f"_process/files/{uuid.uuid4().hex}/{filename}"
    os.makedirs(os.path.dirname(output_path(name)), exist_ok=True)
    return name


def output_path(name: str) -> str:
    """解析可下载文件标识；仅允许交付目录或显式的过程目录。"""
    normalized = name.replace("\\", "/")
    process = normalized.startswith("_process/")
    root = os.path.realpath(_workspace_dir() if process else _deliverables_dir())
    relative = normalized[len("_process/"):] if process else normalized
    path = os.path.realpath(os.path.join(root, relative))
    if os.path.commonpath([root, path]) != root or path == root:
        raise ValueError("文件路径越界")
    return path


def publish_file(name: str, sprint_id: str = "", created_at: float | None = None, project_name: str = "") -> str:
    """仅将最终成品复制到 年-月-日/项目；保留源文件供过程记录引用。"""
    source = safe_deliverable_path(name)
    if not source:
        raise ValueError("待归档的文件不存在")
    day = datetime.fromtimestamp(created_at or time.time(), timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    project = semantic_stem(project_name, "独立生成") if project_name else (re.sub(r"[^\w-]", "", sprint_id) or "独立生成")
    filename = os.path.basename(source)
    folder = output_path(f"{day}/{project}/placeholder")
    with _PUBLISH_LOCK:
        os.makedirs(os.path.dirname(folder), exist_ok=True)
        stem, extension = os.path.splitext(filename)
        target = f"{day}/{project}/{filename}"
        index = 2
        while os.path.exists(output_path(target)):
            target = f"{day}/{project}/{stem} ({index}){extension}"
            index += 1
        path = output_path(target)
        shutil.copy2(source, path)
    return target


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
        base = Path(dest)
        index = 2
        while os.path.exists(dest):
            dest = str(base.with_name(f"{base.stem} ({index}){base.suffix}"))
            index += 1
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
        shutil.copy2(src, output_path(new_name))
        return new_name
    except Exception as error:  # noqa: BLE001
        print(f"[Forge] 复制版本文件失败 {file_name}：{error}")
        return None


def safe_deliverable_path(file_name: str) -> str | None:
    """下载接口用：校验文件名必须落在 deliverables 目录内，防路径穿越"""
    if not file_name:
        return None
    try:
        abs_path = output_path(file_name)
        # 旧事件或正文中的文件标识仍可解析，迁移不破坏历史下载。
        if not os.path.isfile(abs_path) and not file_name.startswith("_process/"):
            legacy = "_process/" + (file_name if file_name.replace("\\", "/").startswith("code/") else "legacy/" + file_name)
            legacy_path = output_path(legacy)
            if os.path.isfile(legacy_path):
                return legacy_path
    except (ValueError, OSError):
        return None
    return abs_path if os.path.isfile(abs_path) else None
