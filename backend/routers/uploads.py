"""附件上传管理路由。

- POST   /api/uploads         上传文件/文件夹
- GET    /api/uploads         列出已上传附件
- GET    /api/uploads/search  基于附件内容问答
- GET    /api/uploads/{id}/raw 读取原始文件（前端图片缩略图预览用）
- DELETE /api/uploads/{id}    删除附件
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse

import file_knowledge
import uploads

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


@router.post("")
async def upload_files(files: list[UploadFile]):
    saved = []
    skipped = []
    for f in files:
        data = await f.read()
        item = uploads.save_upload(f.filename or "unnamed", data)
        if item:
            await asyncio.to_thread(file_knowledge.index_upload, item["id"], item["name"])
        (saved if item else skipped).append(item or (f.filename or "unnamed"))
    return {"uploads": saved, "skipped_too_large": skipped}


@router.get("")
async def get_uploads():
    return {"uploads": uploads.list_uploads()}


@router.get("/search")
async def search_uploads(q: str = "", ids: str = ""):
    selected = [value for value in ids.split(",") if value] if ids else None
    return file_knowledge.answer(q, selected)


@router.get("/{upload_id}/raw")
async def get_upload_raw(upload_id: str):
    """返回附件原始文件，inline 展示供前端图片缩略图预览（路径由 uploads 模块防穿越）"""
    path = uploads.get_upload_path(upload_id)
    if not path:
        raise HTTPException(status_code=404, detail="附件不存在")
    return FileResponse(path, content_disposition_type="inline")


@router.delete("/{upload_id}")
async def remove_upload(upload_id: str):
    if not uploads.delete_upload(upload_id):
        raise HTTPException(status_code=404, detail="附件不存在")
    file_knowledge.remove_upload(upload_id)
    return {"ok": True}
