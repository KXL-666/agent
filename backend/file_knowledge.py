"""Local, source-aware search for uploaded files. No model or network call."""

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import uploads

DB_PATH = Path(uploads.UPLOADS_DIR) / "knowledge.db"
_lock = threading.RLock()


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS chunks (upload_id TEXT, name TEXT, location TEXT, content TEXT, PRIMARY KEY(upload_id, location))")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _segments(upload_id: str):
    path = uploads.get_upload_path(upload_id)
    if not path:
        return []
    suffix = Path(path).suffix.lower()
    try:
        if suffix == '.pdf':
            from pypdf import PdfReader
            return [(f'第 {n} 页', page.extract_text() or '') for n, page in enumerate(PdfReader(path).pages, 1)]
        if suffix == '.docx':
            from docx import Document
            document = Document(path)
            paragraphs = [p.text for p in document.paragraphs]
            paragraphs.extend(' | '.join(c.text for c in row.cells) for table in document.tables for row in table.rows)
            return [('正文', '\n'.join(paragraphs))]
        if suffix == '.xlsx':
            from openpyxl import load_workbook
            workbook = load_workbook(path, read_only=True, data_only=True)
            result = []
            for sheet in workbook.worksheets:
                for row_no, row in enumerate(sheet.iter_rows(values_only=True), 1):
                    value = ' | '.join(str(cell) for cell in row if cell is not None)
                    if value:
                        result.append((f'{sheet.title} 第 {row_no} 行', value))
            workbook.close()
            return result
        if suffix in uploads.TEXT_EXTS:
            with open(path, encoding='utf-8', errors='replace') as stream:
                return [('正文', stream.read())]
    except Exception as error:
        return [('解析失败', str(error)[:160])]
    return []


def _chunks(text: str, size: int = 900, overlap: int = 100):
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return []
    result = []
    for pos in range(0, len(text), size - overlap):
        result.append(text[pos:pos + size])
        if pos + size >= len(text):
            break
    return result


def index_upload(upload_id: str, name: str):
    rows = []
    for location, text in _segments(upload_id):
        for number, chunk in enumerate(_chunks(text), 1):
            rows.append((upload_id, name, f'{location} · 片段 {number}', chunk))
    with _lock, _connect() as conn:
        conn.execute('DELETE FROM chunks WHERE upload_id = ?', (upload_id,))
        conn.executemany('INSERT INTO chunks VALUES (?, ?, ?, ?)', rows)
    return len(rows)


def index_existing():
    with _lock, _connect() as conn:
        known = {row[0] for row in conn.execute('SELECT DISTINCT upload_id FROM chunks')}
    for item in uploads.list_uploads():
        if item['id'] not in known:
            index_upload(item['id'], item['name'])


def remove_upload(upload_id: str):
    with _lock, _connect() as conn:
        conn.execute('DELETE FROM chunks WHERE upload_id = ?', (upload_id,))


def search(query: str, upload_ids: list[str] | None = None, limit: int = 8):
    terms = [term for term in re.split(r'[\s，,。？！?！]+', query.strip()) if term]
    if not terms:
        return []
    # Chinese requests often contain a full sentence; use overlapping two-character terms.
    if all(len(term) > 8 for term in terms):
        common = {'这些', '资料', '文件', '里面', '哪里', '什么', '怎么', '请问', '提到', '有关'}
        expanded = []
        for term in terms:
            for phrase in re.findall(r'[\u4e00-\u9fff]+|[A-Za-z0-9_]{3,}', term):
                if re.fullmatch(r'[\u4e00-\u9fff]+', phrase):
                    expanded.extend(phrase[i:i + 2] for i in range(len(phrase) - 1))
                else:
                    expanded.append(phrase)
        terms = [part for part in dict.fromkeys(expanded) if part not in common][:24]
    if not terms:
        return []
    # A local substring search handles Chinese text without a separate tokenizer.
    clauses = ['(' + ' OR '.join('content LIKE ?' for _ in terms) + ')']
    params: list = [f'%{term}%' for term in terms]
    if upload_ids is not None:
        if not upload_ids:
            return []
        clauses.append('upload_id IN (' + ','.join('?' for _ in upload_ids) + ')')
        params.extend(upload_ids)
    with _lock, _connect() as conn:
        rows = conn.execute('SELECT upload_id, name, location, content FROM chunks WHERE ' + ' AND '.join(clauses), params).fetchall()
    ranked = sorted(rows, key=lambda row: sum(row['content'].lower().count(term.lower()) for term in terms), reverse=True)
    return [dict(row) for row in ranked[:limit]]


def answer(query: str, upload_ids: list[str] | None = None):
    hits = search(query, upload_ids, limit=5)
    return {'answer': '找到以下原文片段，请结合上下文核对。' if hits else '未在已索引文件中找到匹配内容。', 'sources': hits}


def context_for_task(query: str, upload_ids: list[str], limit: int = 12000):
    """Bring relevant chunks from all attached files into the prompt with citations."""
    hits = search(query, upload_ids, limit=16)
    if not hits:
        return uploads.extract_many(upload_ids, total_limit=limit)
    parts = [f"📎《{hit['name']}》{hit['location']}：\n{hit['content']}" for hit in hits]
    return '\n\n'.join(parts)[:limit]
