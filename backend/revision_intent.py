"""Deterministic routing for explicit conversational edit/add/convert requests."""
import re

from fastapi import HTTPException

TYPE_PATTERN = re.compile(r'word|docx|pptx?|powerpoint|幻灯片|演示文稿|excel|xlsx|表格|pdf|html|网页|代码(?:包|项目)?', re.I)


def kinds(text: str) -> list[str]:
    result = []
    for match in TYPE_PATTERN.finditer(text):
        token = match.group().lower()
        kind = ('docx' if token in ('word', 'docx') else 'pptx' if token in ('ppt', 'pptx', 'powerpoint', '幻灯片', '演示文稿')
                else 'xlsx' if token in ('excel', 'xlsx', '表格') else 'html' if token in ('html', '网页')
                else 'pdf' if token == 'pdf' else 'code')
        if kind not in result:
            result.append(kind)
    return result


def infer(feedback: str) -> tuple[str, list[str]]:
    text = feedback.strip()
    if re.search(r'先(?:别|不要|不)(?:修改|生成|执行)|仅讨论|只(?:解释|讨论|分析)|暂时不改|先讨论', text):
        return 'discuss', []
    conversion = re.search(r'(?:转换(?:为|成)|转(?:为|成)|改(?:为|成)|换(?:为|成))\s*(?:一(?:份|个))?', text)
    if conversion:
        targets = kinds(text[conversion.end():])
        if targets:
            if len(targets) != 1:
                raise HTTPException(422, '转换目标有多个，请说明最终要转换成哪一种；新增多种配套文件可以一起提出')
            return 'convert', targets
    addition = re.search(r'(?:再|顺便)(?:帮我|给我)?(?:生成|做|制作|给|输出|配|增加|补)|另外(?:生成|做|配|一份)|额外(?:生成|做|配|一份)|新增|配套|补充一份|补一份', text)
    if addition:
        targets = kinds(text[addition.start():])
        if targets:
            # "再把PPT标题改短" is an edit, not a new output.
            if re.search(r'再把|把.{0,15}(?:标题|颜色|字体|字号|内容|数字|公式)', text) and not re.search(r'新增|配套|另外|额外', text):
                return 'revise', []
            return 'add', targets
    return 'revise', []
