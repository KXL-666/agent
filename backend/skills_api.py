"""Skill configuration and an explicit model trial endpoint."""
import asyncio
import io
import zipfile

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, StrictInt

import llm
import skills
import skill_packages

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillInput(BaseModel):
    name: str = Field(max_length=80)
    description: str = Field(default="", max_length=1000)
    instructions: str = Field(max_length=12000)
    rosters: list[StrictInt] = Field(default_factory=list)
    enabled: bool = True


def _save(body: SkillInput, skill_id: str | None = None):
    try:
        return skills.save_skill(body.model_dump(), skill_id)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except KeyError as error:
        raise HTTPException(404, "技能不存在") from error


@router.get("")
def index():
    return {"skills": skills.list_skills()}


@router.post("")
def create(body: SkillInput):
    return _save(body)


class ImportInput(BaseModel):
    markdown: str = Field(max_length=16000)


@router.post("/import")
def import_markdown(body: ImportInput):
    try:
        return skills.save_skill(skills.parse_markdown(body.markdown))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


class PackageUrlInput(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    roster: int = Field(ge=1, le=6)
    skill_path: str = Field(default="", max_length=300)


@router.post("/install-url")
async def install_url(body: PackageUrlInput):
    try:
        installed = await asyncio.to_thread(skill_packages.install, body.url, body.roster, body.skill_path)
        return {"skills": installed}
    except (ValueError, OSError, httpx.HTTPError) as error:
        raise HTTPException(400, str(error)) from error


@router.post("/install-zip")
async def install_zip(roster: int = Form(ge=1, le=6), file: UploadFile = File()):
    data = await file.read(skill_packages.MAX_ARCHIVE_BYTES + 1)
    if len(data) > skill_packages.MAX_ARCHIVE_BYTES:
        raise HTTPException(400, "技能压缩包超过 30 MB")
    try:
        installed = await asyncio.to_thread(skill_packages.install, data, roster)
        return {"skills": installed}
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error


class RunToolInput(BaseModel):
    roster: int = Field(ge=1, le=6)
    script: str = Field(min_length=1, max_length=300)
    arguments: list[str] = Field(default_factory=list)


@router.post("/{skill_id}/run")
async def run_tool(skill_id: str, body: RunToolInput):
    try:
        return await asyncio.to_thread(skill_packages.run_tool, skill_id, body.roster, body.script, body.arguments)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/{skill_id}/files")
def package_files(skill_id: str, roster: int):
    try:
        return {"files": skill_packages.list_files(skill_id, roster)}
    except ValueError as error:
        raise HTTPException(404, str(error)) from error


@router.get("/{skill_id}/files/{path:path}")
def package_file(skill_id: str, path: str, roster: int):
    try:
        target = skill_packages.asset_file(skill_id, roster, path)
        return FileResponse(target, filename=target.name)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/{skill_id}/repair")
async def repair_package(skill_id: str):
    try:
        return await asyncio.to_thread(skill_packages.repair, skill_id)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/{skill_id}/export")
def export_package(skill_id: str, roster: int):
    try:
        paths = skill_packages.list_files(skill_id, roster)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in paths:
                archive.write(skill_packages.asset_file(skill_id, roster, item["path"]), item["path"])
        return Response(buffer.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="skill-{skill_id}.zip"'})
    except ValueError as error:
        raise HTTPException(404, str(error)) from error


@router.get("/member/{roster}")
def member_preview(roster: int):
    if roster not in range(1, 7):
        raise HTTPException(400, "成员必须在 1–6")
    return {"skills": skills.member_skills(roster),
            "prompt": skills.augment_system("", roster)}


class TrialInput(BaseModel):
    roster: int = Field(ge=1, le=6)
    message: str = Field(min_length=1, max_length=2000)


@router.post("/trial")
async def trial(body: TrialInput):
    if not body.message.strip():
        raise HTTPException(400, "试用任务不能为空")
    if not llm.llm_available(body.roster):
        raise HTTPException(400, "该成员未配置 API，请先到智能体页面配置")
    meta: dict = {}
    reply = await asyncio.to_thread(
        llm.chat_text, "请完成用户的试用任务，按已启用的相关技能工作。",
        [{"role": "user", "content": body.message}], "", roster=body.roster,
        max_tokens=1800, meta=meta,
    )
    if meta.get("fallback_reason"):
        raise HTTPException(502, "模型试用失败：" + meta["fallback_reason"])
    return {"reply": reply, "skills": meta.get("skills", []), "model": meta.get("model")}


@router.put("/{skill_id}")
def update(skill_id: str, body: SkillInput):
    return _save(body, skill_id)


@router.delete("/{skill_id}")
def delete(skill_id: str):
    try:
        skills.delete_skill(skill_id)
    except KeyError as error:
        raise HTTPException(404, "技能不存在") from error
    return {"ok": True}
