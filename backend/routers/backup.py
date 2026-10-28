"""数据备份导入导出路由。

- GET  /api/backup/export  导出全部数据
- POST /api/backup/import  导入备份 zip
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import zipfile

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse

import forge
import skills
import storage
import runtime_state

router = APIRouter(prefix="/api/backup", tags=["backup"])

BACKUP_DB_ITEMS = ("usage.json", "agent_config.json", "pricing.json", "skills_data.json")


@router.get("/export")
async def backup_export():
    """导出全部数据：office.db + 用量/成员配置/单价 + deliverables 产物目录。"""
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    zip_name = forge._unique_name("office-backup.zip")
    zip_path = forge.output_path(zip_name)

    def _pack() -> None:
        with storage._lock:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                if os.path.isfile(storage.DB_PATH):
                    zf.write(storage.DB_PATH, "office.db")
                for name in BACKUP_DB_ITEMS:
                    path = os.path.join(backend_dir, name)
                    if os.path.isfile(path):
                        zf.write(path, name)
                for base in (forge.DELIVERABLES_DIR, forge.WORKSPACE_DIR):
                    for root, _dirs, files in os.walk(base):
                        _dirs[:] = [name for name in _dirs if name not in (".venv", "node_modules", "__pycache__")]
                        if os.path.realpath(root) == os.path.realpath(forge.WORKSPACE_DIR):
                            _dirs[:] = [name for name in _dirs if name not in ("tools", "benchmarks")]
                        for filename in files:
                            full = os.path.join(root, filename)
                            if os.path.abspath(full) == os.path.abspath(zip_path) or filename == "office-backup.zip":
                                continue
                            zf.write(full, os.path.relpath(full, backend_dir))

    await asyncio.to_thread(_pack)
    return FileResponse(zip_path, filename="office-backup.zip", media_type="application/zip")


@router.post("/import")
async def backup_import(file: UploadFile):
    """导入备份 zip：恢复数据库/配置/产物文件。Sprint 运行中拒绝导入。"""
    if runtime_state.running:
        raise HTTPException(status_code=409, detail="Sprint 运行中，请先结束再导入备份")
    data = await file.read()
    if len(data) > 500 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="备份文件超过 500MB，无法导入")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="备份文件不是有效的 zip 包")

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    restored: list[str] = []
    with archive:
        names = set(archive.namelist())

        def _restore() -> None:
            with storage._lock:
                if "office.db" in names:
                    with archive.open("office.db") as src, open(storage.DB_PATH, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    restored.append("office.db")
                for name in BACKUP_DB_ITEMS:
                    if name in names:
                        with archive.open(name) as src, open(
                            os.path.join(backend_dir, name), "wb"
                        ) as dst:
                            shutil.copyfileobj(src, dst)
                        restored.append(name)
                for info in archive.infolist():
                    if info.is_dir() or not info.filename.startswith(("deliverables/", "workspace/")):
                        continue
                    if info.filename.startswith(("workspace/tools/", "workspace/benchmarks/")):
                        continue
                    if any(part in (".venv", "node_modules", "__pycache__") for part in info.filename.replace("\\", "/").split("/")):
                        continue
                    rel = os.path.normpath(info.filename)
                    if ".." in info.filename.replace("\\", "/").split("/") or os.path.commonpath([os.path.realpath(backend_dir), os.path.realpath(os.path.join(backend_dir, rel))]) != os.path.realpath(backend_dir):
                        continue
                    target = os.path.join(backend_dir, rel)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with archive.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    restored.append(rel)

        await asyncio.to_thread(_restore)
    if not restored:
        raise HTTPException(status_code=400, detail="备份包内没有可恢复的数据文件")
    if "skills_data.json" in restored:
        for item in skills.list_skills():
            if item.get("package_id"):
                skills.update_package_status(item["id"], item.get("tools", []),
                                             ["备份恢复后请重新安装依赖"])
    return {
        "ok": True,
        "restored": restored,
        "note": "导入完成。请重启后端并刷新页面；完整技能包的依赖需在技能中心点击重试安装。",
    }
