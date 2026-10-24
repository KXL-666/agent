"""Apply one located repair to a copy of a candidate; preserve other parts."""

import hashlib
from contextlib import closing
import re
import shutil
import uuid
import zipfile
from pathlib import Path

from docx import Document
from openpyxl import load_workbook
from pptx import Presentation

import forge
import project_agent
import artifact_quality


def _destination(sprint_id: str, source: Path) -> Path:
    safe = re.sub(r"[^\w-]", "", sprint_id) or "current"
    relative = f"_process/revisions/{safe}/{uuid.uuid4().hex}/{source.name}"
    path = Path(forge.output_path(relative))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _replace_once(texts: list[tuple[object, str]], old: str, new: str) -> bool:
    matches = [(target, value) for target, value in texts if old in value]
    if len(matches) != 1:
        return False
    target, value = matches[0]
    target.text = value.replace(old, new, 1)
    return True


def apply(sprint_id: str, kind: str, source_file: str, patch: dict,
          project_dir: str = "") -> tuple[str | None, str, dict]:
    source_name = forge.safe_deliverable_path(source_file)
    if not source_name:
        return None, "候选文件不存在", {}
    source = Path(source_name)
    target = _destination(sprint_id, source)
    location = str(patch.get("location") or "").strip()
    replacement = patch.get("replacement")
    if replacement is None or replacement == "":
        return None, "缺少具体替换内容", {}
    detail = {}
    try:
        if kind == "docx":
            old = str(patch.get("old_text") or "")
            if not old:
                return None, "Word 修订需要精确旧文本", {}
            doc = Document(source)
            candidates = [(p, p.text) for p in doc.paragraphs]
            candidates += [(cell, cell.text) for table in doc.tables for row in table.rows for cell in row.cells]
            if not _replace_once(candidates, old, str(replacement)):
                return None, "Word 目标文字不存在或匹配不唯一", {}
            doc.save(target)
            detail = {"location": location or old, "old_text": old}
        elif kind == "pdf":
            old = str(patch.get("old_text") or "")
            source_markdown = artifact_quality.report_directory(source, forge.WORKSPACE_DIR) / "source.md"
            if not old or not source_markdown.is_file():
                return None, "PDF 定点修订需要同源 Markdown 与精确旧文本", {}
            content = source_markdown.read_text(encoding="utf-8")
            if content.count(old) != 1:
                return None, "PDF 源文本目标不存在或匹配不唯一", {}
            updated = content.replace(old, str(replacement), 1)
            from pdf_builder import build_pdf
            if not build_pdf(forge._parse_docx_markdown(updated), str(target)):
                return None, "PDF 修订版生成失败", {}
            target_quality = artifact_quality.report_directory(target, forge.WORKSPACE_DIR)
            target_quality.mkdir(parents=True, exist_ok=True)
            (target_quality / "source.md").write_text(updated, encoding="utf-8")
            detail = {"location": location or old, "old_text": old}
        elif kind == "pptx":
            match = re.search(r"(?:第\s*)?(\d+)\s*(?:页|张|slide)", location, re.I)
            old = str(patch.get("old_text") or "")
            if not match or not old:
                return None, "PPT 修订需要页码和精确旧文本", {}
            deck = Presentation(source)
            index = int(match.group(1)) - 1
            if index < 0 or index >= len(deck.slides):
                return None, "PPT 页码越界", {}
            candidates = [(shape, shape.text) for shape in deck.slides[index].shapes if shape.has_text_frame]
            if not _replace_once(candidates, old, str(replacement)):
                return None, "PPT 目标文字不存在或匹配不唯一", {}
            deck.save(target)
            detail = {"slide": index + 1, "old_text": old}
        elif kind == "xlsx":
            if "!" not in location:
                return None, "Excel 修订需要工作表!单元格位置", {}
            sheet_name, address = location.rsplit("!", 1)
            with closing(load_workbook(source)) as workbook:
                if sheet_name not in workbook or not re.fullmatch(r"\$?[A-Z]{1,3}\$?[1-9]\d*", address):
                    return None, "Excel 工作表或单元格位置无效", {}
                cell = workbook[sheet_name][address]
                old_value = cell.value
                cell.value = replacement
                workbook.save(target)
            detail = {"location": location, "old_value": old_value}
        elif kind == "code":
            relative = str(patch.get("path") or location).replace("\\", "/")
            if not project_dir or not relative:
                return None, "代码修订需要项目目录和文件路径", {}
            original_project = Path(project_dir).resolve()
            if not original_project.is_dir():
                return None, "集成项目源目录不存在", {}
            old_file = project_agent.safe_path(original_project, relative)
            if not old_file.is_file():
                return None, "代码目标文件不存在", {}
            project_copy = target.parent / "project"
            shutil.copytree(original_project, project_copy)
            new_file = project_agent.safe_path(project_copy, relative)
            old_content = new_file.read_text(encoding="utf-8")
            new_content = str(replacement)
            if new_file.name.startswith("test_") and new_content.count("assert") < old_content.count("assert"):
                return None, "返工不得减少测试断言", {}
            new_file.write_text(new_content, encoding="utf-8")
            checks = project_agent.static_check(project_copy)
            if any(check.get("ok") is False for check in checks):
                return None, "修订后的项目静态检查未通过", {"checks": checks}
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in project_agent.project_files(project_copy):
                    archive.write(path, path.relative_to(project_copy).as_posix())
            detail = {"path": relative, "project_dir": str(project_copy),
                      "old_sha256": hashlib.sha256(old_content.encode("utf-8")).hexdigest()}
        else:
            return None, f"{kind} 尚无安全的定点修订器", {}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        return None, f"定点修订失败：{error}", {}
    return "_process/" + target.relative_to(forge.WORKSPACE_DIR).as_posix(), "", detail
