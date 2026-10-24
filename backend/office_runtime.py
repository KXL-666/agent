"""真实 Office 转换与分页渲染。能力缺失会明确返回未验证，不伪造截图。"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def find_office() -> str | None:
    base = Path(__file__).parent
    candidates = [os.getenv("OFFICE_SOFFICE", ""), shutil.which("soffice.com"), shutil.which("soffice")]
    candidates += [str(Path(os.getenv("ProgramFiles", "C:/Program Files")) / "LibreOffice/program/soffice.com")]
    candidates += ["/Applications/LibreOffice.app/Contents/MacOS/soffice"]
    tools = base / "workspace/tools/libreoffice"
    candidates += [str(tools / "program/soffice.com"), str(tools / "program/soffice.exe")]
    found = next((p for p in candidates if p and Path(p).is_file()), None)
    if found:
        return found
    if tools.exists():
        candidates += [str(p) for p in tools.rglob("soffice.com")]
        candidates += [str(p) for p in tools.rglob("soffice.exe")]
    return next((p for p in candidates if p and Path(p).is_file()), None)


def capabilities() -> dict:
    return {"office_renderer": bool(find_office()),
            "pdf_renderer": importlib.util.find_spec("pymupdf") is not None,
            "pdf_writer": importlib.util.find_spec("reportlab") is not None,
            "formula_recalculation": bool(find_office()),
            "code_sandbox": bool(shutil.which("docker"))}


def convert(source: str | Path, directory: str | Path, target: str = "pdf") -> Path:
    executable = find_office()
    if not executable:
        raise RuntimeError("未安装 Office 渲染引擎，无法验证真实分页或重算公式")
    source, directory = Path(source).resolve(), Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if directory == source.parent:
        raise ValueError("转换输出必须与源目录分离")
    with tempfile.TemporaryDirectory(prefix="office-profile-") as profile:
        cmd = [executable, "-env:UserInstallation=" + Path(profile).as_uri(),
               "--headless", "--norestore", "--nodefault", "--nolockcheck",
               "--convert-to", target, "--outdir", str(directory), str(source)]
        result = subprocess.run(cmd, capture_output=True, timeout=120,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    output = directory / (source.stem + "." + target.split(":")[0])
    if result.returncode or not output.is_file() or not output.stat().st_size:
        raise RuntimeError("Office 转换失败：" + (result.stderr or result.stdout).decode("utf-8", errors="replace")[-600:])
    return output


def recalculate(path: str | Path) -> bool:
    """使用真实电子表格引擎计算后原子替换；转换失败保留原文件。"""
    if not find_office():
        return False
    path = Path(path)
    with tempfile.TemporaryDirectory(prefix="office-calc-") as tmp:
        converted = convert(path, tmp, "xlsx:Calc MS Excel 2007 XML")
        # 同一文件系统内替换，Windows 上也不会留下写到一半的成品。
        stage = path.with_suffix(".recalculated.xlsx")
        shutil.copy2(converted, stage)
        os.replace(stage, path)
    return True


def render_pdf(pdf: str | Path, directory: str | Path) -> dict:
    import pymupdf
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    pages, errors, text = [], [], []
    with pymupdf.open(pdf) as document:
        if len(document) > 150:
            raise ValueError("超过 150 页，需拆分后验证")
        for index, page in enumerate(document):
            content = page.get_text()
            text.append(content)
            if "\ufffd" in content:
                errors.append(f"第 {index + 1} 页出现无法识别的字符")
            for x0, y0, x1, y1, *_ in page.get_text("blocks"):
                if x0 < -1 or y0 < -1 or x1 > page.rect.width + 1 or y1 > page.rect.height + 1:
                    errors.append(f"第 {index + 1} 页有内容超出页面边界")
            if not content.strip() and not page.get_images() and not page.get_drawings():
                errors.append(f"第 {index + 1} 页为空白页")
            image = directory / f"page-{index + 1}.png"
            page.get_pixmap(dpi=110, alpha=False).save(image)
            pages.append(image.name)
    return {"pages": pages, "page_count": len(pages), "errors": errors, "text": "\n".join(text)}
