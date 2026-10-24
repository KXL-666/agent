"""识别模型写进交付物的对话式说明；只匹配明确的交付元话语。"""

import re


_META_PATTERNS = (
    re.compile(r"^[（(]?全文\s*(?:约|共|计)?\s*\d+[\d,，]*\s*字[）)]?$"),
    re.compile(r"^(?:说明|注|备注)[:：]\s*(?:以上|本文|本作文|本内容).*(?:作文正文|复制到\s*(?:Word|文档)|示例性素材|真实经历|统计方式|字数)", re.I),
    re.compile(r"(?:以上|以下)(?:是|为).*(?:生成的|作文正文|可直接复制到\s*(?:Word|文档))", re.I),
    re.compile(r"(?:文中|本文).*(?:人物|分数|事件).*(?:示例|虚构|替换)"),
    re.compile(r"(?:如与|若与).*(?:真实经历|实际情况).*(?:不符|替换)"),
    re.compile(r"(?:字数|全文约).*(?:估算口径|统计方式|略有差异)"),
    re.compile(r"AI\s*敏捷开发办公室\s*[·・]\s*自动生成", re.I),
)


def meta_leaks(text: str) -> list[str]:
    """返回明确属于交付解释、字数自报或产品水印的文本行。"""
    return [line.strip()[:200] for line in (text or "").splitlines()
            if line.strip() and any(pattern.search(line.strip()) for pattern in _META_PATTERNS)]


def strip_document_meta(text: str) -> str:
    """只清除文档末尾的交付说明，正文中的引用和业务说明原样保留。"""
    lines = (text or "").rstrip().splitlines()
    while lines:
        last = lines[-1].strip()
        if not last or last in ("---", "***", "___") or meta_leaks(last):
            lines.pop()
        else:
            break
    return "\n".join(lines).rstrip()


def payload_meta_leaks(payload: object) -> list[str]:
    """检查 PPT/表格的标题、单元格、备注等所有可见字符串。"""
    if isinstance(payload, str):
        return meta_leaks(payload)
    if isinstance(payload, dict):
        return [issue for value in payload.values() for issue in payload_meta_leaks(value)]
    if isinstance(payload, (list, tuple)):
        return [issue for value in payload for issue in payload_meta_leaks(value)]
    return []
