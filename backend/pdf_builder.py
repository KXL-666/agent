"""中文可搜索 PDF：分页、书签、重复表头与真实文本，避免截图式 PDF。"""
from pathlib import Path
import os
import shutil
import tempfile
from xml.sax.saxutils import escape


def build_pdf(blocks: list[dict], output: str) -> bool:
    import office_runtime
    if office_runtime.find_office():
        # 与 Word 同源导出，使用成熟排版引擎的中文禁则、分页与字体嵌入能力。
        from forge import _build_docx
        lines, sequence = [], 0
        for block in blocks:
            kind = block["type"]
            sequence = sequence + 1 if kind == "ordered" else 0
            if kind == "table":
                for index, row in enumerate(block["rows"]):
                    lines.append("| " + " | ".join(row) + " |")
                    if index == 0:
                        lines.append("| " + " | ".join("---" for _ in row) + " |")
            elif kind == "code":
                lines.append("```\n" + block["text"] + "\n```")
            else:
                prefix = {"h1": "# ", "h2": "## ", "h3": "### ", "bullet": "- "}.get(kind, f"{sequence}. " if kind == "ordered" else "")
                lines.append(prefix + block.get("text", ""))
            lines.append("")
        with tempfile.TemporaryDirectory(prefix="pdf-layout-") as temporary:
            source = Path(temporary) / "document.docx"
            _build_docx("\n".join(lines), str(source))
            converted = office_runtime.convert(source, Path(temporary) / "pdf", "pdf")
            shutil.copy2(converted, output)
        return Path(output).stat().st_size > 0
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle, Preformatted

    font = "OfficeCJK"
    if font not in pdfmetrics.getRegisteredFontNames():
        candidates = [os.getenv("OFFICE_CJK_FONT", ""), "C:/Windows/Fonts/simsun.ttc",
                      "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf"]
        registered = False
        for path in candidates:
            if not path or not Path(path).is_file():
                continue
            try:
                pdfmetrics.registerFont(TTFont(font, path, subfontIndex=0))
                registered = True
                break
            except Exception:
                continue
        if not registered:
            font = "STSong-Light"
            if font not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(UnicodeCIDFont(font))
    styles = {name: ParagraphStyle(name, fontName=font, fontSize=size, leading=size * 1.6,
                    textColor=colors.HexColor("#182230"), spaceBefore=before, spaceAfter=after,
                    wordWrap="CJK", alignment=TA_LEFT, keepWithNext=keep)
              for name, size, before, after, keep in [
                  ("h1", 23, 0, 18, True), ("h2", 15, 15, 8, True),
                  ("h3", 12, 10, 6, True), ("p", 10.5, 0, 8, False),
                  ("cell", 9, 0, 0, False), ("bullet", 10.5, 0, 5, False)]}
    def paragraph(text, style="p"):
        # Markdown 只作为文本处理；不执行任意 HTML 标签。
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), styles[style])

    story = []
    sequence = 0
    for block in blocks:
        kind = block["type"]
        sequence = sequence + 1 if kind == "ordered" else 0
        if kind == "table":
            rows = block["rows"]
            columns = max(len(row) for row in rows)
            data = [[paragraph(row[c] if c < len(row) else "", "cell") for c in range(columns)] for row in rows]
            table = LongTable(data, colWidths=[(A4[0] - 104) / columns] * columns,
                              repeatRows=1, hAlign="LEFT", splitByRow=1)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DEEBF7")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7FA")]),
                ("GRID", (0, 0), (-1, -1), .5, colors.HexColor("#D9D9D9")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            story.extend([table, Spacer(1, 10)])
        elif kind == "code":
            story.append(Preformatted(block["text"], styles["cell"], maxLineLength=70))
        else:
            prefix = "• " if kind == "bullet" else f"{sequence}. " if kind == "ordered" else ""
            story.append(paragraph(prefix + block.get("text", ""), kind if kind in styles else "p"))

    title = next((b.get("text", "") for b in blocks if b["type"] == "h1"), "文档")
    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.setFillColor(colors.HexColor("#667085"))
        canvas.drawString(52, 30, title[:35])
        canvas.drawRightString(A4[0] - 52, 30, f"{doc.page}")
        canvas.restoreState()

    class BookmarkedDocument(SimpleDocTemplate):
        def afterFlowable(self, flowable):
            if isinstance(flowable, Paragraph) and flowable.style.name in ("h1", "h2", "h3"):
                level = {"h1": 0, "h2": 1, "h3": 2}[flowable.style.name]
                previous = getattr(self, "outline_level", 0)
                level = min(level, previous + 1)
                self.outline_level = level
                key = f"heading-{self.seq.nextf('heading')}"
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(flowable.getPlainText(), key, level=level, closed=False)

    document = BookmarkedDocument(output, pagesize=A4, title=title, author="", leftMargin=52,
                                  rightMargin=52, topMargin=46, bottomMargin=48)
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return Path(output).is_file() and Path(output).stat().st_size > 0
