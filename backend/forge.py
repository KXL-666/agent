"""多格式成品引擎：专用制作、真实文件检查、分页渲染、模型评审和有限修订。

Office/PDF 使用确定性构建器；项目代码经过规划与整合，运行测试交给隔离执行器。
工具不可用时记录未验证范围，不能把文件存在或模型自述当成验收完成。
"""

import html as html_lib
import json
import os
import py_compile
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone, timedelta

from llm import chat_json, chat_text
from artifact_naming import semantic_filename, semantic_stem
from artifact_content import meta_leaks, payload_meta_leaks, strip_document_meta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DELIVERABLES_DIR = os.path.join(BASE_DIR, "deliverables")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
CODE_ROOT = os.path.join(WORKSPACE_DIR, "code")
# 当前 Sprint 的代码目录（每轮独立，避免新任务覆盖历史代码）
CODE_DIR = CODE_ROOT

os.makedirs(DELIVERABLES_DIR, exist_ok=True)
os.makedirs(CODE_DIR, exist_ok=True)

# 统一视觉规范：三类产物共享同一套配色，保证交付物风格一致
COLOR_PRIMARY = "1F4E79"   # 深蓝：封面/页眉带/表头
COLOR_ORANGE = "ED7D31"    # 点缀橙：装饰条
COLOR_LIGHT = "DEEBF7"     # 浅蓝：副标题/隔行
COLOR_GRAY = "808080"      # 辅助灰
FONT_MAIN = "微软雅黑"

_CONTENT_KINDS = ("docx", "pptx", "xlsx", "pdf", "html")
_BUILD_ERROR = threading.local()
_PUBLISH_LOCK = threading.Lock()


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
    root = os.path.realpath(WORKSPACE_DIR if process else DELIVERABLES_DIR)
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


# ===========================================================================
# 内容解析：Markdown / JSON 负载 → 统一中间结构（纯函数，便于测试）
# ===========================================================================

def _strip_fences(text: str) -> str:
    """剥掉模型偶发输出的 ```markdown 围栏"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _clean_inline(text: str) -> str:
    """去掉行内 Markdown 标记（加粗/行内代码/链接），返回纯文本"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return text.strip()


def _bold_segments(text: str) -> list[tuple[str, bool]]:
    """把含 **加粗** 的文本拆成 [(片段, 是否加粗)]，供渲染器分 run 输出"""
    parts = re.split(r"\*\*(.+?)\*\*", text)
    return [
        (_clean_inline(part), bool(i % 2))
        for i, part in enumerate(parts)
        if part
    ]


def _parse_docx_markdown(text: str) -> list[dict]:
    """把 Markdown 文档解析为块序列：h1/h2/h3/p/bullet/table"""
    blocks: list[dict] = []
    table_buf: list[str] = []
    code_lines: list[str] | None = None

    def flush_table() -> None:
        nonlocal table_buf
        if not table_buf:
            return
        rows: list[list[str]] = []
        for row in table_buf:
            cells = [_clean_inline(c) for c in row.strip().strip("|").split("|")]
            # 跳过分隔行 | --- | --- |
            if all(re.fullmatch(r":?-{2,}:?", c or "---") for c in cells):
                continue
            rows.append(cells)
        if rows:
            blocks.append({"type": "table", "rows": rows})
        table_buf = []

    for raw in _strip_fences(text).replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("```"):
            flush_table()
            if code_lines is None:
                code_lines = []
            else:
                blocks.append({"type": "code", "text": "\n".join(code_lines)})
                code_lines = None
            continue
        if code_lines is not None:
            code_lines.append(raw)
            continue
        if line.startswith("|") and line.endswith("|"):
            table_buf.append(line)
            continue
        flush_table()
        if not line or line in ("---", "***", "___"):
            continue
        if line.startswith("### "):
            blocks.append({"type": "h3", "text": _clean_inline(line[4:])})
        elif line.startswith("## "):
            blocks.append({"type": "h2", "text": _clean_inline(line[3:])})
        elif line.startswith("# "):
            blocks.append({"type": "h1", "text": _clean_inline(line[2:])})
        elif re.match(r"^[-*•]\s+", line):
            blocks.append({"type": "bullet", "text": re.sub(r"^[-*•]\s+", "", line)})
        elif re.match(r"^\d+[.、]\s*\S", line):
            blocks.append({"type": "ordered", "text": re.sub(r"^\d+[.、]\s*", "", line)})
        else:
            blocks.append({"type": "p", "text": line})
    flush_table()
    if code_lines is not None:
        blocks.append({"type": "code", "text": "\n".join(code_lines)})
    seen_major = False
    for block in blocks:
        if block["type"] == "h2":
            seen_major = True
        elif block["type"] == "h3" and not seen_major:
            # 不能在一级章节之前出现其子章节，避免正文从三级标题开始。
            block["type"] = "h2"
    return blocks


def _sanitize_table(table) -> dict | None:
    if not isinstance(table, dict):
        return None
    headers = [str(h).strip()[:30] for h in (table.get("headers") or []) if str(h).strip()][:6]
    if len(headers) < 2:
        return None
    rows = []
    for row in (table.get("rows") or [])[:8]:
        if isinstance(row, list):
            cells = ["" if c is None else str(c).strip()[:40] for c in row][: len(headers)]
            cells += [""] * (len(headers) - len(cells))
            rows.append(cells)
    if not rows:
        return None
    return {"headers": headers, "rows": rows}


def _sanitize_pptx(payload, source_spec: str = "") -> dict | None:
    """清洗 LLM 返回的 PPT JSON 负载；结构严重不符时返回 None"""
    if not isinstance(payload, dict):
        return None
    slides_raw = payload.get("slides")
    if not isinstance(slides_raw, list):
        # 兜底：模型换字段名时，取第一个「dict 列表」字段当页序列
        slides_raw = next(
            (
                v for v in payload.values()
                if isinstance(v, list) and v and isinstance(v[0], dict)
            ),
            [],
        )
    slides: list[dict] = []
    for item in slides_raw[:15]:
        if not isinstance(item, dict):
            continue
        heading = str(item.get("heading") or item.get("title") or "").strip()[:40]
        bullets = [
            str(b).strip()
            for b in (item.get("bullets") or item.get("points") or [])
            if str(b).strip()
        ][:7]
        note = str(item.get("note") or "").strip()[:300]
        layout = str(item.get("layout") or "bullets").strip().lower()
        if layout not in ("bullets", "conclusion", "compare", "process", "timeline", "chart", "table"):
            layout = "bullets"
        chart = item.get("chart") if isinstance(item.get("chart"), dict) else None
        if chart:
            categories = chart.get("categories")
            series = chart.get("series")
            source_refs = item.get("source_refs")
            valid = (isinstance(categories, list) and 2 <= len(categories) <= 12
                     and isinstance(series, list) and 1 <= len(series) <= 4
                     and isinstance(source_refs, list) and bool(source_refs))
            normalized = []
            if valid:
                for entry in series:
                    values = entry.get("values") if isinstance(entry, dict) else None
                    if not isinstance(values, list) or len(values) != len(categories) or any(type(v) not in (int, float) for v in values):
                        valid = False
                        break
                    if source_spec and any(not re.search(rf"(?<!\d){re.escape(str(v))}(?!\d)", source_spec) for v in values):
                        valid = False
                        break
                    normalized.append({"name": str(entry.get("name") or "数值")[:30], "values": values})
            chart = {"categories": [str(c)[:30] for c in categories], "series": normalized,
                     "title": str(chart.get("title") or "")[:60]} if valid else None
        if layout == "chart" and not chart:
            layout = "bullets"
        if heading or bullets:
            slides.append({
                "heading": heading or "（未命名章节）",
                "bullets": bullets,
                "table": _sanitize_table(item.get("table")),
                "note": note,
                "layout": layout,
                "claim": str(item.get("claim") or "").strip()[:160],
                "evidence": str(item.get("evidence") or "").strip()[:300],
                "source_refs": [str(ref).strip()[:100] for ref in (item.get("source_refs") or []) if str(ref).strip()][:8],
                "chart": chart,
            })
    if len(slides) < 1:
        return None
    return {
        "title": str(payload.get("title") or "演示文稿").strip()[:40] or "演示文稿",
        "subtitle": str(payload.get("subtitle") or "").strip()[:60],
        "slides": slides,
        "include_cover": bool(payload.get("include_cover", True)),
    }


def _sanitize_xlsx(payload) -> list[dict] | None:
    """清洗 LLM 返回的 Excel JSON 负载为工作表列表；无有效数据返回 None"""
    if not isinstance(payload, dict):
        return None
    sheets_raw = payload.get("sheets")
    if not isinstance(sheets_raw, list):
        sheets_raw = next(
            (
                v for v in payload.values()
                if isinstance(v, list) and v and isinstance(v[0], dict)
            ),
            [],
        )
    sheets: list[dict] = []
    for item in sheets_raw[:4]:
        if not isinstance(item, dict):
            continue
        headers = [str(h).strip()[:30] for h in (item.get("headers") or []) if str(h).strip()][:40]
        if len(headers) < 2:
            continue
        rows: list[list] = []
        for row in (item.get("rows") or [])[:2000]:
            if not isinstance(row, list):
                continue
            cells = [
                c if isinstance(c, (int, float)) else ("" if c is None else str(c).strip()[:2000])
                for c in row
            ][: len(headers)]
            cells += [""] * (len(headers) - len(cells))
            if any(str(c).strip() for c in cells):
                rows.append(cells)
        if len(rows) >= 2:
            sheets.append({
                "name": str(item.get("name") or f"工作表{len(sheets) + 1}").strip()[:20],
                "headers": headers,
                "rows": rows,
                **{key: item[key] for key in ("formats", "column_types", "formulas", "notes") if isinstance(item.get(key), dict)},
                **{key: item[key] for key in ("charts", "validations") if isinstance(item.get(key), list)},
            })
    return sheets or None


# ===========================================================================
# 内容生成 Prompt：全部 token 预算都留给内容本身，不浪费在代码上
# ===========================================================================

def _length_rule(spec: str) -> str:
    """按统一口径给出生成端的硬性字数预算。"""
    import artifact_quality
    budget = artifact_quality.length_budget(spec, "docx")
    if not budget:
        return ""
    return (f"正文汉字数目标 {budget['target']} 字，必须落在 {budget['low']}～{budget['high']} 字之间"
            "（按正文汉字计，不含标题、标点、空格与序号）；超出就压缩句子，不足就补充实质内容，"
            "不得靠标点、空格、重复句子或自报字数凑数。")


def _docx_prose(content: str) -> str:
    """正文文本：不含标题行、表格、列表符号与代码块。"""
    lines, fenced = [], False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced or not stripped or stripped.startswith(("#", "|")):
            continue
        lines.append(stripped)
    return "\n".join(lines)


def _fit_docx_length(content: str, spec: str) -> str:
    """正文超过目标上限时按句确定性压缩；只减不增，优先压缩最长段落。"""
    import artifact_quality
    budget = artifact_quality.length_budget(spec, "docx")
    if not budget:
        return content
    lines = content.splitlines()

    def measure() -> int:
        return artifact_quality.counted_length(_docx_prose("\n".join(lines)))

    if measure() <= budget["high"]:
        return content

    def is_prose(line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and not stripped.startswith(("#", "|", "```")) \
            and not re.match(r"^(?:[-*+>]|\d+[.、])\s*", stripped)

    for _ in range(200):
        if measure() <= budget["high"]:
            break
        prose = [(index, line) for index, line in enumerate(lines) if is_prose(line)]
        if len(prose) <= 1:
            break
        prose.sort(key=lambda item: (-len(item[1]), item[0]))
        trimmed = False
        for index, line in prose:
            sentences = [part for part in re.split(r"(?<=[。！？!?…])", line.strip()) if part]
            if len(sentences) < 2:
                continue
            sentences.pop()
            lines[index] = "".join(sentences)
            trimmed = True
            break
        if not trimmed:
            break
    return "\n".join(lines)


_NUMERIC_STAT = re.compile(r"(?<![\w.])\d{1,4}\s*(?:条|项|个|例|次|人|页|天|%|％|小时|分钟)")


def _pending_data_claim(sentence: str) -> bool:
    """该句没有具体数字，却声称数据待附件/待确认，与正文已给出的统计矛盾。"""
    if re.search(r"\d", sentence):
        return False
    return bool(re.search(r"(?:待|需|须|尚待|尚未)[^。；]{0,20}(?:确认|提供|核验|附件)", sentence)) \
        and bool(re.search(r"数据|信息|情况|结果|分布|统计|附件", sentence))


def _drop_pending_clauses(sentence: str) -> str:
    """删除句中“数据待确认”的分句，保留同一句的结论部分。"""
    clauses = [part for part in re.split(r"([；;])", sentence)]
    # 偶数位是正文分句，奇数位是分隔符
    bodies = clauses[0::2]
    separators = clauses[1::2]
    kept = [part for part in bodies if not _pending_data_claim(part)]
    if not kept:
        return ""
    if not kept[-1].endswith(("。", "！", "？", "!", "?", "…")):
        kept[-1] = kept[-1].rstrip("，, ") + "。"
    rebuilt = kept[0]
    for index in range(1, len(kept)):
        separator = separators[index - 1] if index - 1 < len(separators) else "；"
        rebuilt += separator + kept[index]
    return rebuilt


def _reconcile_pending_claims(content: str) -> str:
    """正文已给出具体数量时，删除“数据需待附件确认”这类自相矛盾表述。"""
    if len(set(_NUMERIC_STAT.findall(_docx_prose(content)))) < 2:
        return content
    kept = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "|", "```")):
            kept.append(line)
            continue
        sentences = [part for part in re.split(r"(?<=[。！？!?…])", stripped) if part]
        updated, changed = [], False
        for part in sentences:
            if not _pending_data_claim(part):
                updated.append(part)
                continue
            changed = True
            remaining = _drop_pending_clauses(part)
            if remaining:
                updated.append(remaining)
        if not changed:
            kept.append(line)
        elif updated:
            kept.append("".join(updated))
    return "\n".join(kept)


def _docx_content_prompt(spec: str, feedback: str = "") -> str:
    return (
        "根据用户需求完成可直接使用的正文，用 Markdown 输出。先判断它是作文、故事、信函、简报、报告、方案、手册还是论文，采用合适结构。"
        "第一行用 # 写具体标题；只有适合分节的文档才使用 ## 和 ###，作文和故事保持自然段。"
        "先给读者最需要的结论、决定或说明，再展开依据和实施细节。"
        "如果提供上一版正文，保留已正确的内容和结构，只修复明确指出的问题，不要重新构思一篇新文档。"
        "不要把所有文档写成六章报告，不要强行凑字数或增加封面；明确要求的字数、章节和受众优先。"
        f"{_length_rule(spec)}"
        "对比、重复记录、数值用表格；长论述保留自然段。步骤可用列表。"
        "保留用户素材、数字、单位、口径和不确定性；不编造来源、统计或完成事项。作文和故事允许合理的虚构情节，直接写成作品，不在正文解释素材是否真实。其他文档中必要的假设应在对应内容旁标明。"
        "不能为了凑字数添加事实或绝对化结论；计算指标可从给定数据推导并注明口径，建议必须写成建议。"
        "正文已经给出具体数字时，不得再写“数据需待附件确认/待补充”这类自相矛盾的表述；数字没有来源就不要写数字。"
        "用户未提供且未要求收集的会议链接、报名方式、日期、署名等信息不要添加字段或待填写占位。"
        "确需说明缺失信息时，在相应事实旁写‘待确认’，不要增设空白表单。"
        "必要的引用在文中使用可识别的来源名称与真实链接。只给成品正文，不附加字数统计、写作说明、复制提示、示例免责声明、工作计划或自评。\n"
        f"需修正的问题：{feedback}\n需求与素材：{spec}"
    )


def _strip_unrequested_docx_placeholders(content: str, spec: str) -> str:
    """Drop invented blank fields in deliverables, preserving requested forms."""
    # 协作汇编 spec 的前缀（"根据需求与团队各环节的实际产出……"）和团队产出正文都可能
    # 含“待确认”，不能把整段 spec 当作需求范围；只截取用户原始需求文本来判定严格清理。
    request_scope = spec.split("团队各环节的产出", 1)[0].split("任务完成情况", 1)[0]
    requirement_match = (
        re.search(r"加工需求[:：]\s*(.+)", request_scope, re.S)
        or re.search(r"需求[:：]\s*(.+)", request_scope, re.S)
    )
    if requirement_match:
        request_scope = requirement_match.group(1)
    if re.search(r"模板|表单|空白表|待填写|请填写|预留填写", request_scope):
        return content
    lines = content.splitlines()
    strict_notice = "通知" in request_scope and "待确认" not in request_scope
    kept = []
    skip_unrequested_section = False
    for line in lines:
        if strict_notice and re.match(r"^\s*#{1,3}\s*(?:[一二三四五六七八九十]+[、.．]\s*)?"
                                      r"(?:需说明的信息|待确认事项|待确认信息)\s*$", line):
            skip_unrequested_section = True
            continue
        if skip_unrequested_section and re.match(r"^\s*#{1,3}\s+", line):
            skip_unrequested_section = False
        if skip_unrequested_section:
            continue
        if strict_notice:
            line = re.sub(r"[（(][^（）()\n]{0,100}待确认[）)]", "", line)
        blank_words = "待补充|待填写|请填写|TBD|TODO" + ("|待确认" if strict_notice else "")
        if re.fullmatch(r"\s*(?:[-*]\s*)?[^：:\n]{1,24}[：:]\s*"
                        rf"(?:{blank_words})\s*", line, re.I):
            continue
        # 独立成行的空白占位（如整行只有“待补充”“待确认”）不是正文。
        if re.fullmatch(rf"\s*(?:[-*]\s*)?(?:{blank_words})[。．.]?\s*", line, re.I):
            continue
        if strict_notice and "待确认" in line:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _pptx_content_prompt(spec: str, feedback: str = "") -> str:
    return (
        "根据以下需求设计一份完整的中文演示文稿，输出一个 JSON 对象：\n"
        '{"title":"演示标题","subtitle":"副标题","slides":[{"heading":"页标题",'
        '"claim":"本页中心结论","layout":"conclusion|compare|process|timeline|chart|table|bullets",'
        '"bullets":["要点1"],"evidence":"依据说明","source_refs":["附件名/用户原话"],'
        '"table":{"headers":["列"],"rows":[["值"]]},'
        '"chart":{"title":"图表标题","categories":["第1周","第2周"],'
        '"series":[{"name":"新增","values":[12,9]}]},"note":"演讲备注"}]}\n'
        "内容要求：\n"
        "1. 先规划逐页故事线，每页一个中心结论；明确页数和封面优先，短需求不要凑页；\n"
        "2. 按内容选结论、双栏对比、流程、时间线、原生图表、表格或简短要点，不强迫固定要点数量；\n"
        "3. 图表只使用用户或附件真实数值，categories/series 的每个值都要有 source_refs；无数据不造图；\n"
        "4. note 写必要的演讲备注；\n"
        "严禁：「略/待补充/示例」式内容、空话套话。\n"
        f"{feedback}\n"
        "事实要求：保留用户素材与关键约束；不编造来源、统计、已完成事项或测试结果；假设与模拟数据必须明确标记。\n"
        "只写观众需要看到的幻灯片和必要演讲备注，不附加生成说明、示例免责声明或产品水印。\n"
        f"需求内容：{spec}"
    )


def _xlsx_content_prompt(spec: str, feedback: str = "") -> str:
    return (
        "设计能实际使用、可编辑且计算逻辑清楚的 Excel 工作簿，只输出 JSON：\n"
        '{"sheets":[{"name":"明细","headers":["项目","数量","单价","金额"],'
        '"rows":[["模拟样例",2,10,"=B2*C2"]],'
        '"column_types":{"数量":"number","单价":"currency","金额":"currency"},'
        '"formats":{"金额":"#,##0.00"},"formulas":{},'
        '"charts":[{"type":"bar","title":"金额对比","category_column":1,"data_columns":[4]}],'
        '"validations":[{"column":"A","options":["选项一","选项二"]}],"notes":{"B2":"数据来源或输入说明"}}]}\n'
        "样例仅说明结构，不能照抄其中的模拟数据。根据需求决定工作表数量，能一张表解决的问题不要拆很多表。"
        "原始输入、假设和计算结果要能区分；统计与汇总使用原生公式，不把算好的数字冒充公式。"
        "公式行号从 2 开始，引用实际存在的单元格；跨表引用加单引号。公式必须能被 Excel/LibreOffice 计算。"
        "图表必须有真实的数值列和分类列，列号从 1 开始。只在能帮助理解或用户要求时添加图表。"
        "日期使用 ISO 日期并声明 date 类型；百分数保存为小数并声明 percent；编号保留文本前导零。"
        "使用上传素材里的全部相关记录，不为达到行数编造数据。缺少输入时明确给出需要填写的字段与说明，"
        "仅在用户允许或明确标记模拟时使用模拟数据。禁止外部工作簿、网络公式或可执行调用。\n"
        "工作表、单元格和备注只写使用者需要的数据与业务说明，不附加生成说明、复制提示或示例免责声明。\n"
        f"需修正的问题：{feedback}\n需求与素材：{spec}"
    )


# ===========================================================================
# 确定性渲染器：docx / pptx / xlsx（不调用 LLM）
# ===========================================================================

def _docx_set_run(run, size: float = 11, bold: bool = False,
                  color: str | None = None, font: str = FONT_MAIN) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    run.font.name = font
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), font)
    run.font.size = Pt(size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def _docx_rich_text(paragraph, text: str, size: float = 11,
                    color: str | None = None) -> None:
    """按 **加粗** 分段输出 run"""
    for segment, bold in _bold_segments(text):
        _docx_set_run(paragraph.add_run(segment), size=size, bold=bold, color=color)


def _docx_shade_cell(cell, fill: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def _docx_page_number(doc) -> None:
    """页脚居中页码（PAGE 域）"""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    footer_p = doc.sections[0].footer.paragraphs[0]
    footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), "18")  # 9pt
    color = OxmlElement("w:color")
    color.set(qn("w:val"), COLOR_GRAY)
    rpr.append(sz)
    rpr.append(color)
    run.append(rpr)
    fld.append(run)
    footer_p._p.append(fld)


def _build_docx(content: str, output_path: str, cover: bool = False) -> bool:
    """原生标题与导航、按内容分页的表格和正文；无需通用封面。"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
    from docx.shared import Pt, Cm
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    blocks = _parse_docx_markdown(content)
    if not blocks:
        return False
    doc = Document()
    for style_name in ("Title", "Heading 1", "Heading 2", "Heading 3"):
        for border in list(doc.styles[style_name]._element.iter(qn("w:pBdr"))):
            border.getparent().remove(border)
    for section in doc.sections:
        section.page_width, section.page_height = Cm(21), Cm(29.7)
        section.top_margin = section.bottom_margin = Cm(2.2)
        section.left_margin = section.right_margin = Cm(2.4)
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3"):
        style = doc.styles[name]
        style.font.name = FONT_MAIN
        style._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), FONT_MAIN)
        fonts = style._element.get_or_add_rPr().rFonts
        for theme in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            fonts.attrib.pop(qn("w:" + theme), None)
        from docx.shared import RGBColor
        style.font.color.rgb = RGBColor(0, 0, 0)
    doc.styles["Normal"].font.size = Pt(10.5)
    doc.styles["Normal"].paragraph_format.line_spacing = 1.45
    doc.styles["Normal"].paragraph_format.space_after = Pt(7)
    for name, size in (("Title", 25), ("Heading 1", 16), ("Heading 2", 13), ("Heading 3", 11)):
        style = doc.styles[name]
        style.font.size = Pt(size)
        style.font.bold = True
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.space_before = Pt(14 if name != "Title" else 0)
        style.paragraph_format.space_after = Pt(8)
    title_used = False
    for block in blocks:
        kind = block["type"]
        if kind in ("h1", "h2", "h3"):
            style = "Title" if kind == "h1" and not title_used else {"h1": "Heading 1", "h2": "Heading 1", "h3": "Heading 2"}[kind]
            heading = doc.add_paragraph(block["text"], style=style)
            for run in heading.runs:
                _docx_set_run(run, size={"Title": 25, "Heading 1": 16, "Heading 2": 13}[style], bold=True, color="000000")
            if cover and style == "Title":
                doc.add_page_break()
            title_used = True
        elif kind == "table":
            rows = block["rows"]
            cols = max(len(r) for r in rows)
            table = doc.add_table(rows=len(rows), cols=cols)
            table.style = "Table Grid"
            table.autofit = False
            weights = [max(3, min(25, max(len(r[c]) if c < len(r) else 0 for r in rows[:15]))) for c in range(cols)]
            for c, weight in enumerate(weights):
                table.columns[c].width = Cm(16.2 * weight / sum(weights))
            for r, row in enumerate(rows):
                properties = table.rows[r]._tr.get_or_add_trPr()
                if r == 0:
                    properties.append(OxmlElement("w:tblHeader"))
                # 超长记录允许跨页，普通记录保持完整。
                if max(map(len, row), default=0) < 350:
                    properties.append(OxmlElement("w:cantSplit"))
                for c, cell in enumerate(table.rows[r].cells):
                    cell.width = Cm(16.2 * weights[c] / sum(weights))
                    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                    text = row[c] if c < len(row) else ""
                    _docx_rich_text(cell.paragraphs[0], text, size=9.5, color="000000")
                    for paragraph in cell.paragraphs:
                        paragraph.paragraph_format.space_after = Pt(5)
                        paragraph.paragraph_format.space_before = Pt(5)
                    if r == 0:
                        _docx_shade_cell(cell, "DEEBF7")
                        for run in cell.paragraphs[0].runs:
                            run.bold = True
                    elif r % 2 == 0:
                        _docx_shade_cell(cell, "F5F7FA")
            doc.add_paragraph().paragraph_format.space_after = Pt(3)
        elif kind in ("bullet", "ordered"):
            paragraph = doc.add_paragraph(style="List Number" if kind == "ordered" else "List Bullet")
            _docx_rich_text(paragraph, block["text"], size=10.5, color="000000")
        elif kind == "code":
            paragraph = doc.add_paragraph()
            run = paragraph.add_run(block["text"])
            run.font.name, run.font.size = "Consolas", Pt(9)
        else:
            paragraph = doc.add_paragraph()
            _docx_rich_text(paragraph, block["text"], size=10.5, color="000000")
    doc.core_properties.title = next((b["text"] for b in blocks if b["type"] == "h1"), "文档")
    doc.core_properties.author = ""
    _docx_page_number(doc)
    doc.save(output_path)
    return os.path.getsize(output_path) > 0


def _ppt_rect(slide, x, y, w, h, fill: str) -> None:
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(fill)
    shape.line.fill.background()
    shape.shadow.inherit = False


def _ppt_text(slide, x, y, w, h, lines, size: float, color: str,
              bold: bool = False, align=None, line_spacing: float = 1.15) -> None:
    """lines: [(text, bold, color)] 或纯 str 列表"""
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    for idx, item in enumerate(lines):
        text, item_bold, item_color = (
            item if isinstance(item, tuple) else (item, bold, color)
        )
        p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
        if align is not None:
            p.alignment = align
        p.line_spacing = line_spacing
        p.space_after = Pt(8)
        run = p.add_run()
        run.text = text
        run.font.name = FONT_MAIN
        run.font.size = Pt(size)
        run.font.bold = item_bold
        run.font.color.rgb = RGBColor.from_string(item_color)


def _build_pptx(payload: dict, output_path: str) -> bool:
    """把 PPT JSON 负载渲染为统一设计风格的 16:9 演示文稿"""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Emu, Inches, Pt

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]
    width, height = prs.slide_width, prs.slide_height

    if payload.get("include_cover", True):
        # ---------- 标题页 ----------
        slide = prs.slides.add_slide(blank)
        _ppt_rect(slide, 0, 0, width, height, COLOR_PRIMARY)
        _ppt_rect(slide, 0, Emu(int(height * 0.68)), width, Pt(6), COLOR_ORANGE)
        _ppt_text(
            slide, Inches(1), Emu(int(height * 0.30)), width - Inches(2), Inches(1.6),
            [payload["title"]], 40, "FFFFFF", bold=True, align=PP_ALIGN.CENTER,
        )
        if payload.get("subtitle"):
            _ppt_text(
                slide, Inches(1), Emu(int(height * 0.50)), width - Inches(2), Inches(0.8),
                [payload["subtitle"]], 18, COLOR_LIGHT, align=PP_ALIGN.CENTER,
            )

    # ---------- 内容页 ----------
    has_cover = bool(payload.get("include_cover", True))
    total = len(payload["slides"]) + int(has_cover)
    for content_index, item in enumerate(payload["slides"], 1):
        page_number = content_index + int(has_cover)
        slide = prs.slides.add_slide(blank)
        # 顶部标题带 + 点缀条
        _ppt_rect(slide, 0, 0, width, Inches(1.0), COLOR_PRIMARY)
        _ppt_rect(slide, 0, Inches(1.0), Inches(2.4), Pt(4), COLOR_ORANGE)
        _ppt_text(
            slide, Inches(0.6), Inches(0.12), width - Inches(1.2), Inches(0.8),
            [item["heading"]], 26, "FFFFFF", bold=True,
        )
        from presentation_layout import render_body
        render_body(slide, item)
        # 演讲备注
        if item.get("note"):
            slide.notes_slide.notes_text_frame.text = item["note"]
        # 页脚：左侧文档名 + 右侧页码
        _ppt_text(
            slide, Inches(0.5), height - Inches(0.45), Inches(6), Inches(0.35),
            [payload["title"]], 10, COLOR_GRAY,
        )
        _ppt_text(
            slide, width - Inches(2.6), height - Inches(0.45), Inches(2.1), Inches(0.35),
            [f"第 {page_number} 页 / 共 {total} 页"], 10, COLOR_GRAY, align=PP_ALIGN.RIGHT,
        )

    try:
        prs.save(output_path)
        return os.path.getsize(output_path) > 0
    except OSError as error:
        print(f"[Forge] pptx 保存失败：{error}")
        return False


def _build_xlsx(sheets: list[dict], output_path: str) -> bool:
    from spreadsheet_builder import build_workbook
    return build_workbook(sheets, output_path)


def _gen_docx(spec: str, roster: int | None, feedback: str = "", generation_meta: dict | None = None) -> str:
    _BUILD_ERROR.sections = []
    long_form = bool(re.search(r"长篇|白皮书|论文|多章节|不少于\s*[2-9]\d{3}\s*字|至少\s*[2-9]\d{3}\s*字", spec[:1500]))
    if long_form and not feedback:
        plan = chat_json(
            "你是文档主编。先规划长文档章节，不写正文。输出 JSON："
            '{"title":"具体标题","sections":[{"heading":"章节标题","goal":"本节要解决的问题"}]}。'
            "最多 6 节，遵守用户明确要求；不能把计划写成已完成。",
            spec[:18000], fallback={}, roster=roster, max_tokens=1600, timeout=120,
            meta=generation_meta, fallback_roster=1 if roster != 1 else None,
        )
        sections = plan.get("sections") if isinstance(plan, dict) else None
        if isinstance(sections, list) and 2 <= len(sections) <= 6:
            pieces = []
            for section in sections:
                if not isinstance(section, dict) or not section.get("heading"):
                    pieces = []
                    break
                heading = str(section["heading"]).strip()[:80]
                body = _strip_fences(chat_text(
                    "你是文档作者。只写本节可直接交付的正文，不写计划、生成说明或虚构事实。"
                    "如给定资料只说明计划，不能把它写成已完成。不要重复其他章节。",
                    [{"role": "user", "content": json.dumps({"document_title": plan.get("title"),
                        "section_heading": heading, "section_goal": section.get("goal", ""),
                        "all_sections": [s.get("heading") for s in sections if isinstance(s, dict)],
                        "requirement_and_sources": spec[:22000]}, ensure_ascii=False)}],
                    fallback="", temperature=.4, max_tokens=5500, timeout=300, roster=roster,
                ))
                if not body:
                    pieces = []
                    break
                pieces.append({"heading": heading, "body": body})
            if pieces:
                _BUILD_ERROR.sections = pieces
                title = str(plan.get("title") or "文档")[:100]
                return "# " + title + "\n\n" + "\n\n".join(
                    f"## {part['heading']}\n\n{part['body']}" for part in pieces
                )
    draft = _strip_fences(chat_text(
        "你是专业文档作者，完成实际内容并遵守用户的格式和篇幅要求。",
        [{"role": "user", "content": _docx_content_prompt(spec, feedback)}],
        fallback="", temperature=.5, max_tokens=12000, timeout=300, roster=roster,
        meta=generation_meta, fallback_roster=1 if roster == 6 else None))
    return draft


def _gen_pptx(spec: str, roster: int | None, feedback: str = "",
              generation_meta: dict | None = None) -> dict | None:
    import artifact_quality
    requested = artifact_quality.contract(spec, "pptx")
    prompt = _pptx_content_prompt(spec, feedback)
    explicit = requested.get("unit") == "页"
    scope = artifact_quality.requirement_scope(spec)
    include_cover = requested["cover"] or (not explicit and "封面" not in scope)
    content_pages = None
    if explicit:
        is_content_page_count = bool(re.search(r"\d{1,5}\s*页(?:内容页|内容)", scope[:300]))
        amount = requested["count"] if is_content_page_count else requested["count"] - int(include_cover)
        amount = max(1, amount)
        content_pages = amount
        prompt = prompt.replace("8~12 页内容页（不含标题页）", f"{max(1, amount)} 页内容页")
        prompt += f"\n用户明确指定页数；只生成 {max(1, amount)} 个 slides 元素。不要自行扩展页数。"
    first_payload = chat_json(
        "你是演示文稿设计师，只输出完整 JSON。", prompt,
        fallback={}, temperature=.5, max_tokens=10000, timeout=300, roster=roster,
        meta=generation_meta, fallback_roster=1 if roster != 1 else None)
    payload = _sanitize_pptx(first_payload, spec)
    if content_pages is not None and payload and len(payload["slides"]) != content_pages:
        correction_prompt = (
            f"首次结果有 {len(payload['slides'])} 张内容页，但用户明确要求 {content_pages} 张内容页。"
            "请修订以下 JSON，严格输出指定数量的 slides；合并重复或次要内容，保留所有关键结论、用户数据、"
            "来源和必要风险，不得添加虚构内容，也不要增加封面（封面由渲染器单独处理）。"
            f"\n需求：{spec}\n首次结果：{json.dumps(first_payload, ensure_ascii=False)}"
        )
        corrected = chat_json(
            "你是演示文稿编辑，按精确页数修订 JSON。", correction_prompt,
            fallback={}, temperature=.25, max_tokens=10000, timeout=300, roster=roster,
            meta=generation_meta, fallback_roster=1 if roster != 1 else None,
        )
        corrected_payload = _sanitize_pptx(corrected, spec)
        if corrected_payload and len(corrected_payload["slides"]) == content_pages:
            payload = corrected_payload
    if payload:
        payload["include_cover"] = include_cover
    return payload


def _gen_xlsx(spec: str, roster: int | None, feedback: str = "",
              generation_meta: dict | None = None) -> list[dict] | None:
    from spreadsheet_contract import budget_workbook
    source = spec.split("附件内容：\n", 1)[1].split("\n\n加工需求", 1)[0] if "附件内容：\n" in spec else spec.split("团队各环节的产出", 1)[0]
    grounded = budget_workbook(source)
    if grounded:
        return grounded
    return _sanitize_xlsx(chat_json(
        "你是电子表格建模专家，只输出完整 JSON。", _xlsx_content_prompt(spec, feedback),
        fallback={}, temperature=.4, max_tokens=14000, timeout=300, roster=roster,
        meta=generation_meta, fallback_roster=1 if roster != 1 else None))


def _gen_html(spec: str, roster: int | None, feedback: str = "",
              generation_meta: dict | None = None) -> str:
    """让 LLM 直接产出完整可运行的 HTML 文件（含内联 CSS/JS/SVG）。"""
    prompt = (
        "你是资深前端工程师。根据需求输出一个完整、可直接在浏览器打开运行的 HTML 文件。"
        "要求：\n"
        "1. 只输出 HTML 源码本身，不要用 ```html 代码块包裹，不要写任何解释文字；\n"
        "2. 所有 CSS 和 JavaScript 必须内联在同一个 HTML 文件中，不依赖外部资源；\n"
        "3. 如涉及 SVG/Canvas 动画，必须确保动画能正常运行，代码完整无缺；\n"
        "4. 页面要有合理的视觉样式，居中布局，适配常见屏幕；\n"
        "5. 不得包含 TODO、占位符、生成说明或模型对话痕迹。"
    )
    user_content = spec
    if feedback:
        user_content += f"\n\n上一轮反馈（请针对性修改）：\n{feedback}"
    raw = chat_text(
        prompt,
        [{"role": "user", "content": user_content}],
        fallback="", temperature=.5, max_tokens=16000, timeout=300, roster=roster,
        meta=generation_meta, fallback_roster=1 if roster != 1 else None,
    )
    text = (raw or "").strip()
    # 去掉模型可能添加的 markdown 代码围栏
    if text.startswith("```"):
        text = re.sub(r"^```(?:html)?\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def _forge_once(kind: str, spec: str, output_path: str,
                roster: int | None, feedback: str = "", generation_meta: dict | None = None) -> bool:
    """单轮「生成内容 → 渲染」；失败返回 False（不残留半成品文件）"""
    _BUILD_ERROR.message = ""
    try:
        if kind in ("docx", "pdf"):
            content = strip_document_meta(_gen_docx(spec, roster, feedback, generation_meta))
            content = _strip_unrequested_docx_placeholders(content, spec)
            content = _reconcile_pending_claims(content)
            content = _fit_docx_length(content, spec)
            leaks = meta_leaks(content)
            if leaks:
                _BUILD_ERROR.message = "成品正文含生成说明或字数自报，请只返回正文：" + "；".join(leaks[:3])
                return False
            if kind == "pdf":
                from pdf_builder import build_pdf
                built = bool(content) and build_pdf(_parse_docx_markdown(content), output_path)
            else:
                import artifact_quality
                built = bool(content) and _build_docx(content, output_path, cover=artifact_quality.contract(spec, kind)["cover"])
            if built:
                import artifact_quality
                directory = artifact_quality.report_directory(output_path, WORKSPACE_DIR)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "source.md").write_text(content, encoding="utf-8")
                for index, section in enumerate(getattr(_BUILD_ERROR, "sections", []), 1):
                    section_dir = directory / "sections"
                    section_dir.mkdir(exist_ok=True)
                    (section_dir / f"{index:02d}.md").write_text(
                        f"## {section['heading']}\n\n{section['body']}", encoding="utf-8")
            return built
        if kind == "pptx":
            payload = _gen_pptx(spec, roster, feedback, generation_meta)
            leaks = payload_meta_leaks(payload)
            if leaks:
                _BUILD_ERROR.message = "演示文稿含生成说明，请删除：" + "；".join(leaks[:3])
                return False
            built = payload is not None and _build_pptx(payload, output_path)
            if built:
                import artifact_quality
                directory = artifact_quality.report_directory(output_path, WORKSPACE_DIR)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "source.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return built
        if kind == "xlsx":
            sheets = _gen_xlsx(spec, roster, feedback, generation_meta)
            leaks = payload_meta_leaks(sheets)
            if leaks:
                _BUILD_ERROR.message = "表格含生成说明，请删除：" + "；".join(leaks[:3])
                return False
            built = sheets is not None and _build_xlsx(sheets, output_path)
            if built:
                import artifact_quality
                directory = artifact_quality.report_directory(output_path, WORKSPACE_DIR)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "source.json").write_text(json.dumps({"sheets": sheets}, ensure_ascii=False), encoding="utf-8")
            return built
        if kind == "html":
            content = _gen_html(spec, roster, feedback, generation_meta)
            if not content or not content.strip():
                _BUILD_ERROR.message = "HTML 内容为空"
                return False
            if meta_leaks(content):
                _BUILD_ERROR.message = "HTML 含生成说明或模型对话痕迹，请只返回可直接运行的 HTML"
                return False
            try:
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(content)
                built = True
            except OSError as error:
                _BUILD_ERROR.message = f"HTML 落盘失败：{error}"
                built = False
            if built:
                import artifact_quality
                directory = artifact_quality.report_directory(output_path, WORKSPACE_DIR)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "source.html").write_text(content, encoding="utf-8")
            return built
    except Exception as error:  # noqa: BLE001 —— 单类产物失败不影响整体流程
        _BUILD_ERROR.message = str(error)[:2000]
        print(f"[Forge] {kind} 生成异常：{error}")
    return False


def forge_file(kind: str, spec: str, filename: str, roster: int | None = None,
               generation_meta: dict | None = None, *, quick: bool = False) -> str | None:
    """制作 -> 真实文件检查 -> 成品评审 -> 按问题修订；保留最佳候选。

    quick=True 时跳过模型评审（model_review）并减少重试次数，用于快速生成模式。
    """
    import artifact_quality
    if kind not in _CONTENT_KINDS:
        return None
    best_name, best_report, feedback = None, None, ""
    attempts = []
    max_attempts = 2 if quick else 3
    for attempt in range(max_attempts):
        candidate = _unique_name(filename)
        candidate_path = output_path(candidate)
        attempt_meta = {}
        generated = (
            _forge_once(kind, spec, candidate_path, roster, feedback, attempt_meta)
            if generation_meta is not None
            else _forge_once(kind, spec, candidate_path, roster, feedback)
        )
        if not generated:
            if generation_meta is not None:
                generation_meta.update(attempt_meta)
            feedback = getattr(_BUILD_ERROR, "message", "") or "未生成可用文件，请检查内容结构并返回完整内容。"
            attempts.append({"attempt": attempt + 1, "stage": "build", "status": "failed", "detail": feedback})
            continue
        # 保存完整需求，检查的是实际文件而不是模型自述。
        report = artifact_quality.inspect(candidate_path, kind, spec, WORKSPACE_DIR)
        # 快速模式或本地自检全通过时，跳过模型评审以节省一次 LLM 调用
        if not quick and not any(c["ok"] is False for c in report["checks"]):
            report = artifact_quality.model_review(report, candidate_path, WORKSPACE_DIR, roster)
        attempts.append({"attempt": attempt + 1, "stage": "review", "status": report["status"]})
        def score_of(result):
            return (sum(c["ok"] is False for c in result["checks"]),
                    len(result.get("review", {}).get("issues", [])) if result.get("review", {}).get("status") == "failed" else 0,
                    sum(c["ok"] is None for c in result["checks"]))
        score = score_of(report)
        old_score = score_of(best_report) if best_report else None
        if best_report is None or score <= old_score:
            best_name, best_report = candidate, report
            if generation_meta is not None:
                generation_meta.update(attempt_meta)
        feedback = artifact_quality.feedback(report)
        if report["ok"] or not feedback:
            break
        directory = artifact_quality.report_directory(candidate_path, WORKSPACE_DIR)
        previous = next((path for path in (directory / "source.md", directory / "source.json") if path.is_file()), None)
        if previous:
            feedback += "\n\n上一版完整内容或结构（以它为基准定点修订，保留正确部分）：\n" + previous.read_text(encoding="utf-8")[:30000]
    if best_name:
        best_report["attempts"] = attempts
        artifact_quality.save_report(output_path(best_name), WORKSPACE_DIR, best_report)
    return best_name


# ===========================================================================
# 代码类产物：LLM 直接写源文件（===FILE 标记格式，只落盘不执行）
# ===========================================================================

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
    target_dir = os.path.join(CODE_DIR, subdir) if subdir else CODE_DIR
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
            written.append("_process/" + os.path.relpath(abs_path, WORKSPACE_DIR).replace("\\", "/"))
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


def verify_generated_code(code_dir: str | None = None) -> list[str]:
    """对产出代码做基础可运行性检查：Python 语法编译 + Node 语法检查"""
    failures: list[str] = []
    root = Path(code_dir or CODE_DIR)
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
    root = Path(code_dir or CODE_DIR).resolve()
    try:
        selection = json.loads((root / ".selected-project.json").read_text(encoding="utf-8"))
        chosen = (root / selection["directory"]).resolve()
        if chosen.is_relative_to(root) and chosen.is_dir():
            return str(chosen)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return str(root)


def project_report(code_dir: str | None = None) -> dict:
    root = Path(code_dir or CODE_DIR)
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
    create_project(requirement, CODE_DIR, 5)
    root = Path(selected_project())
    return [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]


def render_docx_from_markdown(content: str, filename: str) -> str | None:
    """确定性渲染：把 Markdown 文本渲染为带封面/样式/页码的真实 docx（不调用 LLM）

    用于演示模式与产物站内文本编辑（编辑后确定性重渲染为新版本文件）。"""
    filename = _unique_name(filename)
    file_path = output_path(filename)
    if _build_docx(content, file_path):
        import artifact_quality
        directory = artifact_quality.report_directory(file_path, WORKSPACE_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "source.md").write_text(content, encoding="utf-8")
        return filename
    print(f"[Forge] Markdown 渲染 docx 失败：{filename}")
    return None


def pdf_from_docx(docx_name: str, filename: str = "同源文档.pdf") -> str | None:
    """Convert the exact Word candidate to searchable PDF without rewriting text."""
    import artifact_quality
    import office_runtime

    source = safe_deliverable_path(docx_name)
    if not source:
        return None
    pdf_name = _unique_name(filename)
    destination = output_path(pdf_name)
    try:
        with tempfile.TemporaryDirectory(prefix="word-pdf-") as directory:
            converted = office_runtime.convert(source, directory, "pdf")
            shutil.copy2(converted, destination)
        source_markdown = artifact_quality.report_directory(source, WORKSPACE_DIR) / "source.md"
        if source_markdown.is_file():
            target_quality = artifact_quality.report_directory(destination, WORKSPACE_DIR)
            target_quality.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_markdown, target_quality / "source.md")
        return pdf_name
    except (OSError, RuntimeError, ValueError) as error:
        print(f"[Forge] Word 同源 PDF 转换失败：{error}")
        return None


def render_document_from_markdown(content: str, filename: str) -> str | None:
    if not filename.lower().endswith(".pdf"):
        name = render_docx_from_markdown(content, filename)
    else:
        from pdf_builder import build_pdf
        name = _unique_name(filename)
        if not build_pdf(_parse_docx_markdown(content), output_path(name)):
            return None
    if name:
        import artifact_quality
        directory = artifact_quality.report_directory(output_path(name), WORKSPACE_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "source.md").write_text(content, encoding="utf-8")
        artifact_quality.inspect(output_path(name), Path(filename).suffix.lstrip("."), "", WORKSPACE_DIR)
    return name


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


def extract_preview(file_name: str, limit: int = 1500) -> str:
    """从生成的真实文件中提取文本，作为产物中心的正文预览"""
    path = safe_deliverable_path(file_name)
    if not path:
        return ""
    if file_name.lower().endswith(".pdf"):
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)[:limit]
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

            with closing(load_workbook(path, read_only=True, data_only=True)) as wb:
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

            with closing(load_workbook(path, read_only=True, data_only=True)) as wb:
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
    import artifact_quality
    path = safe_deliverable_path(file_name)
    if not path:
        return {"ok": False, "status": "failed", "checks": [{"label": "文件存在", "ok": False, "detail": "文件不存在"}]}
    existing = artifact_quality.load_report(path, WORKSPACE_DIR)
    if existing and existing.get("kind") in _CONTENT_KINDS:
        import office_runtime
        if existing.get("capabilities") != office_runtime.capabilities():
            return artifact_quality.inspect(path, existing["kind"], existing.get("requirement", ""), WORKSPACE_DIR)
    return existing or artifact_quality.inspect(path, kind or Path(file_name).suffix.lstrip("."), "", WORKSPACE_DIR)


def self_check_note(result: dict) -> str:
    """把自检结果格式化为可直接附在产物正文后的 Markdown 小节"""
    lines = ["", "", "## 成品自检", ""]
    for item in result.get("checks", []):
        mark = "✅" if item["ok"] is True else "未验证" if item["ok"] is None else "⚠️"
        lines.append(f"- {mark} {item['label']}：{item['detail']}")
    review = result.get("review", {})
    if review.get("status") in ("passed", "failed"):
        lines.append("- 模型成品评审：" + ("通过" if review["status"] == "passed" else "需修正"))
        lines.extend("- " + str(issue) for issue in review.get("issues", []))
    if not result.get("ok"):
        lines.append("")
        lines.append("> 请查看未通过项或未验证项；缺少工具能力时，重新生成不会替代实际验证。")
    return "\n".join(lines)
