"""为交付文件和项目目录生成可读、安全的名称。"""

import re


def semantic_stem(requirement: str, fallback: str = "项目") -> str:
    """从用户需求提取短名称，不把保存路径写进名称。"""
    text = (requirement or "").strip()
    named = re.search(r"(?:文件名|命名为|标题(?:为|叫)?)\s*[:：叫为]?\s*[《「『\"']?([^》」』\"'，。；;\n]+)", text)
    quoted = re.search(r"《([^》]+)》", text) or re.search(r"[「『]([^」』]+)[」』]", text)
    if named:
        text = named.group(1)
    elif quoted:
        text = quoted.group(1)
    else:
        text = re.split(r"[，,。；;\n]|(?:并且|同时|然后|要求|包含|需要|保存到|放到|放在|输出到|生成到)", text, 1)[0]
        text = re.sub(r"^(?:请|帮我|给我|为我|麻烦|生成|制作|创建|写|撰写|设计|开发|做|导出|输出|建立|新建|整理|编写)+\s*", "", text)
        text = re.sub(r"^(?:(?:一篇|一份|一个|一套|一张|关于|以|名为|题为|主题为)\s*)+", "", text)
        text = re.sub(r"(?:的)?(?:作文|文章|报告|总结|周报|演示文稿|幻灯片|PPT|Word|Excel|PDF|docx|pptx|xlsx|文档|文件|表格|代码包|源代码|代码项目)(?:文件|文档)?\s*$", "", text, flags=re.I)
        text = re.sub(r"\s*(?:生成|制作|创建|写成|导出|输出|保存)(?:为|成)?\s*(?:一篇|一份|一个|一张)?\s*(?:Word|Excel|PDF|PPT|docx|pptx|xlsx|文档|文件|表格|演示文稿).*", "", text, flags=re.I)
    text = re.sub(r"\.(?:docx|pptx|xlsx|pdf|zip)$", "", text, flags=re.I)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip(" .-_\t\n")
    text = re.sub(r"\s+", " ", text)[:48].rstrip(" .-_―")
    if not text or text.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", text, re.I):
        return fallback
    return text


def semantic_filename(requirement: str, extension: str, fallback: str = "项目") -> str:
    return f"{semantic_stem(requirement, fallback)}.{extension.lstrip('.')}"
