"""Conversation API: explicit edit/discuss/rollback, persistent jobs and recovery."""
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import revision_jobs
import revision_service
import runtime_state
import storage
import revision_outputs
import uploads

router = APIRouter(prefix='/api/revision', tags=['revision'])


class SessionRequest(BaseModel):
    artifact_id: str | None = None
    target_kinds: list[Literal['docx', 'pdf', 'pptx', 'xlsx', 'html', 'code']] = Field(default=['docx'], min_length=1, max_length=6)
    upload_ids: list[str] = Field(default=[], max_length=30)
    budget: float = Field(default=0, ge=0, allow_inf_nan=False)
    execution_mode: Literal['cloud', 'local'] = 'cloud'


class MessageRequest(BaseModel):
    feedback: str = Field(min_length=1, max_length=6000)
    request_id: str = Field(min_length=1, max_length=100)
    expected_artifact_id: str
    action: Literal['auto', 'generate', 'revise', 'discuss', 'add', 'convert'] = 'auto'
    mode: Literal['auto', 'targeted', 'rewrite'] = 'auto'
    target_kind: Literal['docx', 'pdf', 'pptx', 'xlsx', 'html', 'code'] | None = None
    target_kinds: list[Literal['docx', 'pdf', 'pptx', 'xlsx', 'html', 'code']] = Field(default=[], max_length=6)
    artifact_id: str | None = None


class RollbackRequest(BaseModel):
    target_id: str
    request_id: str = Field(min_length=1, max_length=100)
    expected_artifact_id: str


@router.post('/session')
async def create_session(req: SessionRequest):
    if not req.artifact_id:
        if len(set(req.target_kinds)) != len(req.target_kinds):
            raise HTTPException(422, '交付类型不能重复')
        known = {item['id'] for item in uploads.list_uploads()}
        if not set(req.upload_ids) <= known:
            raise HTTPException(422, '部分附件已不存在，请重新选择')
        if req.execution_mode == 'local':
            from route_helpers import _local_model_ready
            if not _local_model_ready():
                raise HTTPException(422, '本地模式要求所有已配置的主、备用模型API位于本机')
        session = storage.create_revision_conversation(req.model_dump(exclude={'artifact_id'}))
        return revision_jobs.session_snapshot(session['id'])
    artifact = storage.get_artifact(req.artifact_id)
    if not artifact:
        raise HTTPException(404, '产物不存在')
    if (artifact.get('regen') or {}).get('bundle_pending'):
        raise HTTPException(409, '该产物组尚未完成，请从会话中的完整快照继续编辑')
    if not artifact.get('file_name'):
        raise HTTPException(422, '请先生成真实文件，再开始对话修改')
    if revision_service.kind_of(artifact) not in revision_service.FILE_KINDS | {'code'}:
        raise HTTPException(422, '该文件类型暂不支持对话修改')
    linked = (artifact.get('regen') or {}).get('bundle_anchor')
    artifact = storage.get_artifact(linked) if linked else artifact
    if not artifact:
        raise HTTPException(404, '产物组主版本不存在')
    artifact = revision_outputs.bind_existing(artifact)
    session = storage.create_revision_session(artifact['id'])
    return revision_jobs.session_snapshot(session['id'])


@router.get('/sessions')
async def list_sessions(artifact_id: str | None = None):
    if not artifact_id:
        return {'sessions': [{**s, 'title': (storage.get_sprint(s['sprint_id']) or {}).get('requirement', '对话生成')}
                             for s in storage.list_revision_sessions()]}
    artifact = storage.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, '产物不存在')
    root = storage.artifact_root(artifact['sprint_id'], artifact_id)
    matched = storage.list_revision_sessions(root)
    for session in storage.list_revision_sessions():
        if any(output['artifact_id'] == artifact_id for output in revision_outputs.output_snapshot(storage.get_artifact(session['artifact_id']))) and session['id'] not in {s['id'] for s in matched}:
            matched.append(session)
    return {'sessions': sorted(matched, key=lambda s: s['updated_at'], reverse=True)}


@router.get('/session/{session_id}')
async def get_session(session_id: str):
    return revision_jobs.session_snapshot(session_id)


def _submit(session_id: str, request_id: str, expected_id: str, action: str, payload: dict) -> dict:
    # Idempotent retries may retrieve the existing result even while busy.
    if runtime_state.running:
        raise HTTPException(409, 'Sprint 仍在运行，结束后再修改或讨论产物')
    try:
        operation, created = storage.begin_revision_operation(session_id, request_id, expected_id, action, payload)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if created:
        revision_jobs.dispatch(operation)
    return {'operation': operation, 'session_id': session_id}


@router.post('/session/{session_id}/message', status_code=202)
async def message(session_id: str, req: MessageRequest):
    if not req.feedback.strip():
        raise HTTPException(400, '请填写具体修改要求或问题')
    session = storage.get_revision_session(session_id)
    if not session:
        raise HTTPException(404, '会话不存在')
    previous = next((op for op in storage.revision_operations(session_id) if op['request_id'] == req.request_id), None)
    raw_request = req.model_dump(exclude={'request_id', 'expected_artifact_id'})
    if previous:
        if previous['payload'].get('_request') != raw_request:
            raise HTTPException(409, '同一请求标识不能用于不同操作')
        return _submit(session_id, req.request_id, req.expected_artifact_id, previous['action'],
                       previous['payload'])
    action, targets = req.action, req.target_kinds or ([req.target_kind] if req.target_kind else [])
    if action == 'auto':
        import revision_intent
        action, inferred = revision_intent.infer(req.feedback)
        targets = targets or inferred
    action = 'generate' if not session['artifact_id'] and action in ('revise', 'generate') else action
    if not session['artifact_id'] and action != 'generate':
        raise HTTPException(422, '请先生成首版文件')
    if session['artifact_id'] and action == 'generate':
        raise HTTPException(409, '首版已生成，请继续修改或新增配套文件')
    if action in ('add', 'convert') and not targets:
        raise HTTPException(422, '请选择目标文件类型')
    if action == 'add':
        existing = {output['kind'] for output in revision_outputs.output_snapshot(
            storage.get_artifact(session['artifact_id']))}
        if existing.intersection(targets):
            raise HTTPException(409, '当前快照已有该类型文件，请选中已有文件修改；未发起模型调用')
    return _submit(session_id, req.request_id, req.expected_artifact_id, action,
                   {'feedback': req.feedback.strip(), 'mode': req.mode,
                    'target_kind': targets[0] if targets else None, 'target_kinds': targets,
                    'artifact_id': req.artifact_id, '_request': raw_request})


@router.post('/session/{session_id}/rollback', status_code=202)
async def rollback(session_id: str, req: RollbackRequest):
    session = storage.get_revision_session(session_id)
    if not session:
        raise HTTPException(404, '会话不存在')
    target = storage.get_artifact(req.target_id)
    if not target or target.get('sprint_id') != session['sprint_id'] or storage.artifact_root(session['sprint_id'], req.target_id) != session['root_id']:
        raise HTTPException(400, '目标版本不在当前版本链')
    return _submit(session_id, req.request_id, req.expected_artifact_id, 'rollback',
                   {'target_id': req.target_id, 'feedback': f'恢复 v{target.get("version", 1)} 的内容'})


@router.get('/operations/{operation_id}')
async def operation(operation_id: str):
    result = storage.get_revision_operation(operation_id)
    if not result:
        raise HTTPException(404, '修订任务不存在')
    return {'operation': result}


@router.get('/diff/{artifact_id}')
async def diff(artifact_id: str, base_id: str):
    import asyncio
    import revision_diff
    return await asyncio.to_thread(revision_diff.compare, artifact_id, base_id)


@router.get('/snapshot/{artifact_id}')
async def snapshot(artifact_id: str):
    artifact = storage.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, '版本不存在')
    return {'outputs': revision_outputs.output_snapshot(artifact)}


@router.get('/visual/{artifact_id}')
async def visual_diff(artifact_id: str, base_id: str):
    import asyncio
    import revision_visual
    return await asyncio.to_thread(revision_visual.compare, artifact_id, base_id)


@router.get('/visual/{artifact_id}/{base_id}/{render_id}/pages/{page}/{side}')
async def visual_page(artifact_id: str, base_id: str, render_id: str, page: int, side: Literal['before', 'after']):
    import revision_visual
    from fastapi.responses import FileResponse
    return FileResponse(revision_visual.page_path(artifact_id, base_id, render_id, page, side), media_type='image/png')
