"""用户偏好档案路由。

- GET    /api/preferences            列出全部偏好
- POST   /api/preferences {text}     新增偏好（去重，上限 30 条）
- DELETE /api/preferences/{item_id}  删除指定偏好
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import user_preferences

router = APIRouter(tags=["preferences"])


class PreferenceIn(BaseModel):
    text: str


@router.get("/api/preferences")
async def list_preferences():
    return {"preferences": user_preferences.list_items()}


@router.post("/api/preferences")
async def add_preference(payload: PreferenceIn):
    try:
        return user_preferences.add(payload.text)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.delete("/api/preferences/{item_id}")
async def delete_preference(item_id: str):
    if not user_preferences.remove(item_id):
        raise HTTPException(status_code=404, detail="偏好不存在")
    return {"ok": True}
