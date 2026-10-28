"""Rendered-page pixel differences with bounded region annotations and caching."""
import hashlib
import json
import tempfile
from collections import deque
from pathlib import Path

from fastapi import HTTPException
from PIL import Image, ImageChops, ImageDraw, ImageFilter

import artifact_quality
import forge
import office_runtime
import storage


def page_regions(before: Path | None, after: Path | None, directory: Path, number: int) -> dict:
    left = Image.open(before).convert('RGB') if before else None
    right = Image.open(after).convert('RGB') if after else None
    width = max(left.width if left else 0, right.width if right else 0)
    height = max(left.height if left else 0, right.height if right else 0)
    if not width or not height or width * height > 20000000:
        raise HTTPException(422, '页面图片尺寸无效或超过2000万像素')
    a, b = Image.new('RGB', (width, height), 'white'), Image.new('RGB', (width, height), 'white')
    if left: a.paste(left, (0, 0))
    if right: b.paste(right, (0, 0))
    difference = ImageChops.difference(a, b)
    channels = difference.split()
    maximum = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    mask = maximum.point(lambda value: 255 if value >= 22 else 0)
    count = mask.histogram()[255]
    regions = []
    if before is None or after is None:
        regions = [[0, 0, width, height]]
    elif count:
        cell = max(8, max(width, height) // 180)
        dilated = mask.filter(ImageFilter.MaxFilter(5))
        occupied = set()
        for y in range(0, height, cell):
            for x in range(0, width, cell):
                if dilated.crop((x, y, min(x + cell, width), min(y + cell, height))).getbbox():
                    occupied.add((x // cell, y // cell))
        while occupied:
            first = occupied.pop(); queue = deque([first]); cluster = [first]
            while queue:
                x, y = queue.popleft()
                for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if neighbor in occupied:
                        occupied.remove(neighbor); queue.append(neighbor); cluster.append(neighbor)
            regions.append([max(0, min(x for x, _ in cluster) * cell - 3),
                            max(0, min(y for _, y in cluster) * cell - 3),
                            min(width, (max(x for x, _ in cluster) + 1) * cell + 3),
                            min(height, (max(y for _, y in cluster) + 1) * cell + 3)])
    regions.sort(key=lambda r: (r[1], r[0]))
    directory.mkdir(parents=True, exist_ok=True)
    files = {}
    for side, canvas in (('before', a), ('after', b)):
        draw = ImageDraw.Draw(canvas)
        for index, rectangle in enumerate(regions, 1):
            x0, y0, x1, y1 = rectangle
            draw.rectangle((x0, y0, max(x0, x1 - 1), max(y0, y1 - 1)), outline='#e33b30', width=3)
            draw.text((x0 + 3, y0 + 3), str(index), fill='#d82018')
        filename = f'page-{number}-{side}.png'; canvas.save(directory / filename); files[side] = filename
    return {'page': number, 'width': width, 'height': height, 'regions': regions,
            'changed_pixels': count, 'changed_fraction': count / (width * height),
            'status': 'added' if before is None else 'removed' if after is None else 'changed' if regions else 'unchanged',
            'files': files}


def _pages(artifact: dict) -> list[Path]:
    path = forge.safe_deliverable_path(artifact.get('file_name') or '')
    if not path:
        raise HTTPException(404, '页面对比文件已丢失')
    report = artifact_quality.load_report(path, forge.WORKSPACE_DIR) or {}
    directory = artifact_quality.report_directory(path, forge.WORKSPACE_DIR)
    pages = [directory / name for name in report.get('pages', [])]
    if pages and all(page.is_file() for page in pages):
        return pages
    kind = Path(path).suffix.lower()
    if kind not in ('.pdf', '.docx', '.pptx', '.xlsx'):
        raise RuntimeError('此类型没有分页渲染器，可使用完整内容与格式差异')
    directory = directory / 'visual-comparison'
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / 'pages.json'
    if manifest.is_file():
        saved = json.loads(manifest.read_text(encoding='utf-8'))
        pages = [directory / name for name in saved]
        if pages and all(page.is_file() for page in pages):
            return pages
    if kind == '.pdf':
        rendered = office_runtime.render_pdf(path, directory)
    else:
        with tempfile.TemporaryDirectory(prefix='revision-visual-') as temporary:
            pdf = office_runtime.convert(path, temporary, 'pdf')
            rendered = office_runtime.render_pdf(pdf, directory)
    names = rendered.get('pages', [])
    if not names:
        raise RuntimeError('渲染器未返回实际页面')
    manifest.write_text(json.dumps(names), encoding='utf-8')
    return [directory / name for name in names]


def _directory(current: dict, base: dict) -> Path:
    paths = [forge.safe_deliverable_path(a.get('file_name') or '') for a in (base, current)]
    if not all(paths):
        raise HTTPException(404, '版本文件已丢失')
    hashes = [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths]
    # A pair of 64-character hashes plus the render digest exceeds Windows'
    # legacy path limit in ordinary user/temp workspaces.
    pair = hashlib.sha256('_'.join(hashes).encode('ascii')).hexdigest()[:32]
    return Path(forge.WORKSPACE_DIR) / 'revision_visual' / pair


def artifacts_pair(artifact_id: str, base_id: str):
    current, base = storage.get_artifact(artifact_id), storage.get_artifact(base_id)
    if not current or not base:
        raise HTTPException(404, '版本不存在')
    if current['sprint_id'] != base['sprint_id']:
        raise HTTPException(400, '不能比较不同任务的页面')
    return current, base


def compare(artifact_id: str, base_id: str) -> dict:
    current, base = artifacts_pair(artifact_id, base_id)
    directory = _directory(current, base)
    try:
        before, after = _pages(base), _pages(current)
        # Include rendered image bytes in the cache identity: a font/runtime
        # change may alter pixels even when the Office file bytes are identical.
        digest = hashlib.sha256(b''.join(hashlib.sha256(p.read_bytes()).digest() for p in before + after)).hexdigest()
        manifest = directory / (digest + '.json')
        if manifest.is_file():
            result = json.loads(manifest.read_text(encoding='utf-8'))
            if all((directory / digest / page['files'][side]).is_file() for page in result['pages'] for side in ('before', 'after')):
                return {**result, 'artifact_id': artifact_id, 'base_id': base_id}
        pages = [page_regions(before[index] if index < len(before) else None,
                              after[index] if index < len(after) else None, directory / digest, index + 1)
                 for index in range(max(len(before), len(after)))]
        result = {'status': 'ready', 'artifact_id': artifact_id, 'base_id': base_id,
                  'render_id': digest, 'pages': pages, 'changed_pages': sum(p['status'] != 'unchanged' for p in pages),
                  'note': '红框标注像素变化区域；采用同页码对齐，不判断变化的语义好坏。'}
        directory.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return result
    except (RuntimeError, OSError, ValueError, ImportError) as error:
        return {'status': 'unavailable', 'artifact_id': artifact_id, 'base_id': base_id, 'pages': [], 'reason': str(error)}


def page_path(artifact_id: str, base_id: str, render_id: str, page: int, side: str) -> Path:
    import re
    current, base = artifacts_pair(artifact_id, base_id)
    if not re.fullmatch('[0-9a-f]{64}', render_id) or page < 1 or side not in ('before', 'after'):
        raise HTTPException(400, '页面参数无效')
    path = _directory(current, base) / render_id / f'page-{page}-{side}.png'
    if not path.is_file():
        raise HTTPException(404, '变化区域图片尚未生成')
    return path
