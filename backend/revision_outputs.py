"""Version snapshots for a conversation's source files and derived companions."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import HTTPException

import artifact_quality
import artifacts
import forge
import llm
import revision_service as service
import storage
from verification import source_material

KINDS = service.FILE_KINDS | {'code'}


def canonical(artifact: dict) -> dict:
    anchor_id = (artifact.get('regen') or {}).get('bundle_anchor')
    anchor = storage.get_artifact(anchor_id) if anchor_id else artifact
    if not anchor or anchor.get('sprint_id') != artifact.get('sprint_id'):
        raise HTTPException(404, '产物组主版本已丢失')
    return anchor


def members(anchor: dict, *, discover=False) -> list[dict]:
    saved = (anchor.get('regen') or {}).get('bundle_members')
    if saved:
        records = copy.deepcopy(saved)
        # Previous conversation versions recorded parent links but not content
        # dependencies. Adopt an exact in-bundle parent only; never guess by type.
        for index, record in enumerate(records):
            item = storage.get_artifact(record['artifact_id']) or {}
            parent = next((r for r in records if r['artifact_id'] == item.get('parent_id')), None)
            if index == 0 and 'sync_from' not in record:
                record['sync_from'] = None
            if index > 0 and parent and not record.get('derived_from') and 'sync_from' not in record:
                record['sync_from'] = parent['slot']
                record['sync_instruction'] = (item.get('regen') or {}).get('revision_contract', {}).get('latest_feedback', '')
        return records
    result = [{'slot': 'source', 'artifact_id': anchor['id'], 'derived_from': None, 'sync_from': None}]
    # Adopt only proven same-source Word/PDF pairs. Do not guess by filenames.
    if discover and service.kind_of(anchor) == 'docx':
        path = forge.safe_deliverable_path(anchor.get('file_name') or '')
        source = artifact_quality.report_directory(path, forge.WORKSPACE_DIR) / 'source.md' if path else None
        if source and source.is_file():
            matches = []
            for item in storage.list_artifacts(anchor['sprint_id']):
                if item.get('kind') != 'final' or service.kind_of(item) != 'pdf':
                    continue
                other = forge.safe_deliverable_path(item.get('file_name') or '')
                evidence = artifact_quality.report_directory(other, forge.WORKSPACE_DIR) / 'source.md' if other else None
                if evidence and evidence.is_file() and source.read_bytes() == evidence.read_bytes():
                    matches.append(item)
            if matches:
                pdf = min(matches, key=lambda item: abs(item['created_at'] - anchor['created_at']))
                result.append({'slot': 'pdf', 'artifact_id': pdf['id'], 'derived_from': 'source'})
    return result


def bind_existing(anchor: dict) -> dict:
    if service.kind_of(anchor) == 'pdf':
        for item in storage.list_artifacts(anchor['sprint_id']):
            if item.get('kind') == 'final' and service.kind_of(item) == 'docx':
                paired = members(item, discover=True)
                if any(member['artifact_id'] == anchor['id'] for member in paired):
                    anchor = item
                    break
    records = members(anchor, discover=True)
    if len(records) > 1 and not (anchor.get('regen') or {}).get('bundle_members'):
        anchor = artifacts.update_artifact(anchor['id'], regen={**(anchor.get('regen') or {}), 'bundle_members': records})
    return anchor


def output_snapshot(anchor: dict | None) -> list[dict]:
    if not anchor:
        return []
    result = []
    quality = (anchor.get('regen') or {}).get('bundle_member_quality', {})
    for member in members(anchor):
        item = storage.get_artifact(member['artifact_id'])
        if item:
            status = quality.get(item['id'], item.get('quality_status', 'unchecked'))
            result.append({**member, 'artifact': {**item, 'quality_status': status,
                'degraded_reason': '' if status == 'passed' else item.get('degraded_reason', '')},
                'snapshot_quality_status': item.get('quality_status'), 'kind': service.kind_of(item)})
    return result


def select(anchor: dict, artifact_id: str | None) -> tuple[dict, list[dict], str]:
    records = members(anchor, discover=True)
    chosen = artifact_id or anchor['id']
    record = next((r for r in records if r['artifact_id'] == chosen), None)
    if not record:
        raise HTTPException(409, '所选文件不属于当前版本快照，请刷新')
    # PDF is edited through its native source, keeping the two files aligned.
    if record.get('derived_from'):
        record = next((r for r in records if r['slot'] == record['derived_from']), None)
        if record is None:
            raise HTTPException(422, '配套文件缺少源文件关系')
    item = storage.get_artifact(record['artifact_id'])
    if not item:
        raise HTTPException(404, '当前版本源文件已丢失')
    return item, records, record['slot']


async def _create(sid: str, kind: str, contract: dict, *, source: dict | None = None,
                  derived=False, parent_id: str | None = None, operation_id: str | None = None,
                  version=1) -> dict:
    from sprint_runner import _manual_call_guard, _persist_manual_usage_delta, _usage_snapshot
    before, token = _usage_snapshot(), _manual_call_guard(sid)
    try:
        regen = {}
        if derived and source:
            if kind != 'pdf' or service.kind_of(source) not in ('docx', 'pptx', 'xlsx'):
                raise HTTPException(422, '同源导出目前用于Office源文件与PDF')
            if service.kind_of(source) == 'docx':
                name = await asyncio.to_thread(forge.pdf_from_docx, source['file_name'], f'配套-{uuid.uuid4().hex[:8]}.pdf')
            else:
                import office_runtime
                name = f'_process/revisions/{uuid.uuid4().hex}/配套.pdf'
                Path(forge.output_path(name)).parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory() as directory:
                    converted = await asyncio.to_thread(office_runtime.convert,
                        forge.safe_deliverable_path(source['file_name']), directory, 'pdf')
                    shutil.copy2(converted, forge.output_path(name))
            if not name:
                raise HTTPException(422, '同源PDF转换失败；请检查Office引擎，旧文件及旧快照已保留')
            regen['derived_source_sha256'] = (source.get('regen') or {}).get('sha256') or hashlib.sha256(
                Path(forge.safe_deliverable_path(source['file_name'])).read_bytes()).hexdigest()
        elif source:
            contract = service.format_contract(contract, kind)
            context = await asyncio.to_thread(service.source_context, source)
            contract['attachment_context'] = str(contract.get('attachment_context') or '') + (
                f'\n📎《制作来源 {source["id"]}》当前文件正文：\n' + context)
            fields = await service._rewrite(source, contract, contract.get('latest_feedback', ''), kind)
            name = fields['file_name']; regen.update(fields.get('regen') or {})
        elif kind == 'code':
            import revision_code
            fields = await asyncio.to_thread(revision_code.build, source_material(contract))
            name = fields['file_name']; regen.update(fields.get('regen') or {})
        else:
            name = await asyncio.to_thread(forge.forge_file, kind, source_material(contract),
                                          f'对话成品-{uuid.uuid4().hex[:8]}.{kind}', 3)
        path = forge.safe_deliverable_path(name or '')
        if not path:
            raise HTTPException(502, '未生成可用文件，旧快照未改变')
        regen.update(mode='manual_revision', file_kind=kind, revision_contract=contract,
                     sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(), bundle_pending=True,
                     revision_operation_id=operation_id)
        item = artifacts.add_artifact('final', f'对话成品 · {kind.upper()} v{version}', 3,
            await asyncio.to_thread(forge.extract_preview, name, 16000), file_name=name,
            quality_status='unchecked', regen=regen, parent_id=parent_id, version=version, sprint_id=sid)
        return item
    finally:
        llm.reset_call_guard(token)
        _persist_manual_usage_delta(sid, before)


async def _seal(anchor: dict, records: list[dict], changed: list[dict], summary: str) -> dict:
    by_slot = {record['slot']: record for record in records}
    for record in records:
        seen, current = set(), record
        while current.get('derived_from') or current.get('sync_from'):
            if current['slot'] in seen:
                raise HTTPException(422, '配套文件依赖形成循环，未提交快照')
            seen.add(current['slot'])
            current = by_slot.get(current.get('derived_from') or current.get('sync_from'))
            if current is None:
                raise HTTPException(422, '配套文件依赖缺失，未提交快照')
    regen = {**(anchor.get('regen') or {}), 'bundle_members': records, 'bundle_pending': True}
    regen.pop('bundle_anchor', None)
    artifacts.update_artifact(anchor['id'], regen=regen)
    for item in changed:
        await service._validate(item, anchor['sprint_id'])
    actual = [storage.get_artifact(record['artifact_id']) for record in records]
    def member_status(item):
        if not item: return 'unchecked'
        return (item.get('regen') or {}).get('bundle_member_quality', {}).get(item['id'], item.get('quality_status', 'unchecked'))
    failed = any(not item or item.get('degraded') or member_status(item) == 'failed' for item in actual)
    passed = not failed and all(member_status(item) == 'passed' for item in actual)
    status = 'passed' if passed else 'failed' if failed else 'unchecked'
    regen.update(bundle_pending=False, bundle_status=status,
                 bundle_member_quality={item['id']: member_status(item) for item in actual if item},
                 revision_summary=summary)
    # Store the contract next to each snapshot. The session pointer advances
    # only after all companions were created; partial bundles cannot be adopted.
    anchor = artifacts.update_artifact(anchor['id'], regen=regen, quality_status=status,
        degraded_reason='' if passed else '产物组仍有验收问题' if failed else '产物组存在未验证项')
    for item in changed:
        if item['id'] != anchor['id']:
            artifacts.update_artifact(item['id'], regen={**(item.get('regen') or {}),
                'bundle_pending': False, 'bundle_anchor': anchor['id']})
    return {'ok': True, 'artifact': anchor, 'summary': summary, 'outputs': output_snapshot(anchor)}


async def generate(session: dict, feedback: str, contract: dict, operation_id: str) -> dict:
    settings = session.get('settings') or {}
    kinds = settings.get('target_kinds') or ['docx']
    kinds = sorted(kinds, key=lambda kind: kind == 'pdf')
    records, changed = [], []
    anchor = None
    for kind in kinds:
        source_record = next((r for r in records if service.kind_of(storage.get_artifact(r['artifact_id'])) in ('docx', 'pptx', 'xlsx')), None)
        if kind != 'pdf':
            source_record = records[0] if records else None
        source = storage.get_artifact(source_record['artifact_id']) if source_record else None
        output_contract = {**contract, 'target_kind': kind}
        derived = bool(source) and kind == 'pdf' and service.kind_of(source) in ('docx', 'pptx', 'xlsx')
        item = await _create(session['sprint_id'], kind, output_contract, source=source, derived=derived,
                             operation_id=operation_id)
        anchor = anchor or item
        slot = kind
        records.append({'slot': slot, 'artifact_id': item['id'], 'derived_from': source_record['slot'] if derived else None,
                        'sync_from': source_record['slot'] if source and not derived else None,
                        'sync_instruction': feedback if source and not derived else None})
        changed.append(item)
    storage.update_sprint(session['sprint_id'], requirement=feedback, deliver_kind=kinds[0], stage='对话生成')
    storage.save_contract(session['sprint_id'], contract)
    return await _seal(anchor, records, changed, '已生成并绑定：' + '、'.join(k.upper() for k in kinds))


async def edit(anchor: dict, feedback: str, contract: dict, *, operation_id: str,
               action='revise', mode='auto', target_kind: str | None = None, artifact_id: str | None = None,
               target_kinds: list[str] | None = None) -> dict:
    source, records, slot = select(anchor, artifact_id)
    sid = anchor['sprint_id']
    version = storage.latest_artifact_version(sid, storage.artifact_root(sid, anchor['id'])) + 1
    if action == 'add' and target_kinds and len(target_kinds) > 1:
        if len(set(target_kinds)) != len(target_kinds):
            raise HTTPException(422, '配套类型不能重复')
        existing = {service.kind_of(storage.get_artifact(r['artifact_id'])) for r in records}
        if existing.intersection(target_kinds):
            raise HTTPException(409, '部分配套类型已存在，请选中已有文件修改')
        changed = []
        for kind in target_kinds:
            item = await _create(sid, kind, {**contract, 'target_kind': kind}, source=source,
                derived=kind == 'pdf' and service.kind_of(source) in ('docx', 'pptx', 'xlsx'),
                parent_id=anchor['id'], operation_id=operation_id, version=version)
            derived = kind == 'pdf' and service.kind_of(source) in ('docx', 'pptx', 'xlsx')
            records.append({'slot': kind + '-' + uuid.uuid4().hex[:6], 'artifact_id': item['id'],
                'derived_from': slot if derived else None, 'sync_from': None if derived else slot,
                'sync_instruction': feedback})
            changed.append(item)
        return await _seal(changed[-1], records, changed, '已新增配套文件：' + '、'.join(target_kinds))
    if action in ('add', 'convert'):
        if target_kind not in KINDS:
            raise HTTPException(422, '请选择要生成的文件类型')
        if action == 'add' and any(service.kind_of(storage.get_artifact(r['artifact_id'])) == target_kind for r in records):
            raise HTTPException(409, '当前快照已有该类型文件，请选中后修改，或使用类型转换')
        derived = target_kind == 'pdf' and service.kind_of(source) in ('docx', 'pptx', 'xlsx')
        revised = await _create(sid, target_kind, contract, source=source, derived=derived,
            parent_id=anchor['id'], operation_id=operation_id, version=version)
        if action == 'add' or derived:
            new_slot = target_kind + '-' + uuid.uuid4().hex[:6]
            records.append({'slot': new_slot, 'artifact_id': revised['id'], 'derived_from': slot if derived else None,
                            'sync_from': slot if action == 'add' and not derived else None,
                            'sync_instruction': feedback if action == 'add' and not derived else None})
        else:
            next(r for r in records if r['slot'] == slot)['artifact_id'] = revised['id']
        changed = [revised]
        summary = ('已新增配套产物：' if action == 'add' else '已生成转换版本：') + target_kind.upper()
    else:
        result = await service.revise(source['id'], feedback, contract=contract, mode=mode,
            parent_id=anchor['id'], operation_id=operation_id, defer_validation=True)
        revised = result['artifact']; changed = [revised]; summary = result['summary']
        next(r for r in records if r['slot'] == slot)['artifact_id'] = revised['id']
    if action != 'add':
        await sync_descendants(sid, records, slot, revised, contract, operation_id, version, changed)
    if len(changed) > 1:
        summary += f'；同步更新{len(changed) - 1}个配套文件'
    return await _seal(revised, records, changed, summary)


async def sync_descendants(sid: str, records: list[dict], slot: str, revised: dict, contract: dict,
                           operation_id: str, version: int, changed: list[dict]) -> None:
    pending = {slot}
    for _ in range(len(records)):
        next_pending = set()
        for record in records:
            dependency = record.get('derived_from') or record.get('sync_from')
            if dependency not in pending or record['artifact_id'] == revised['id']:
                continue
            parent = next(r for r in records if r['slot'] == dependency)
            old = storage.get_artifact(record['artifact_id'])
            source = storage.get_artifact(parent['artifact_id'])
            if record.get('derived_from'):
                item = await _create(sid, service.kind_of(old), {**contract, 'target_kind': service.kind_of(old)},
                    source=source, derived=True, parent_id=old['id'], operation_id=operation_id, version=version)
            else:
                item = await sync_content(sid, old, source, contract, operation_id, version)
            record['artifact_id'] = item['id']; changed.append(item); next_pending.add(record['slot'])
        pending = next_pending
        if not pending:
            break


async def sync_content(sid: str, old: dict, source: dict, source_contract: dict,
                       operation_id: str, version: int) -> dict:
    """Update facts from the new source while retaining the companion's edits."""
    from sprint_runner import _manual_call_guard, _persist_manual_usage_delta, _usage_snapshot
    before, token = _usage_snapshot(), _manual_call_guard(sid)
    try:
        feedback = source_contract.get('latest_feedback') or '同步更新源文件中的内容，保留本文件未要求修改的结构与格式'
        effective = await asyncio.to_thread(service.evolve_contract, old, feedback)
        effective = service.format_contract(effective, service.kind_of(old))
        source_text = await asyncio.to_thread(service.source_context, source)
        source_hash = hashlib.sha256(Path(forge.safe_deliverable_path(source['file_name'])).read_bytes()).hexdigest()
        reference = f'📎《联动源文件 {source["id"]}》SHA256 {source_hash}：\n{source_text}'
        effective['attachment_context'] = str(effective.get('attachment_context') or '') + '\n' + reference
        instruction = feedback + '\n重要：按更新后的源文件同步事实、数值和结论；保留当前配套文件中未受影响的内容与样式。\n' + reference
        fields = await service._rewrite(old, effective, instruction)
        name = fields['file_name']
        path = forge.safe_deliverable_path(name)
        regen = {**(old.get('regen') or {}), **(fields.get('regen') or {}), 'bundle_pending': True,
                 'mode': 'manual_revision', 'file_kind': service.kind_of(old), 'revision_operation_id': operation_id,
                 'revision_contract': effective, 'sync_source_artifact_id': source['id'],
                 'sync_source_sha256': source_hash, 'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()}
        return artifacts.add_artifact('final', f'内容同步 · {service.kind_of(old).upper()} v{version}', old.get('author_roster'),
            await asyncio.to_thread(forge.extract_preview, name, 16000), file_name=name, regen=regen,
            parent_id=old['id'], version=version, sprint_id=sid, quality_status='unchecked')
    finally:
        llm.reset_call_guard(token); _persist_manual_usage_delta(sid, before)


async def replace_document(anchor: dict, source: dict, filename: str, content: str, operation_id: str | None = None) -> dict:
    source, records, slot = select(anchor, source['id'])
    sid, operation_id = anchor['sprint_id'], operation_id or uuid.uuid4().hex
    version = storage.latest_artifact_version(sid, storage.artifact_root(sid, anchor['id'])) + 1
    contract = service.snapshot_contract(source)
    regen = {**(source.get('regen') or {}), 'mode': 'manual_revision', 'file_kind': service.kind_of(source), 'bundle_pending': True,
             'revision_operation_id': operation_id, 'revision_contract': contract,
             'sha256': hashlib.sha256(Path(forge.safe_deliverable_path(filename)).read_bytes()).hexdigest()}
    revised = artifacts.add_artifact('final', f'编辑快照 v{version}', source.get('author_roster'), content,
        file_name=filename, parent_id=anchor['id'], version=version, regen=regen,
        quality_status='unchecked', source='local', sprint_id=sid)
    next(r for r in records if r['slot'] == slot)['artifact_id'] = revised['id']
    changed = [revised]
    await sync_descendants(sid, records, slot, revised, contract, operation_id, version, changed)
    return await _seal(revised, records, changed, '已保存编辑内容并同步配套文件')


async def rollback(anchor: dict, target: dict, operation_id: str) -> dict:
    records, changed = members(target, discover=True), []
    restored = None
    version = storage.latest_artifact_version(anchor['sprint_id'], storage.artifact_root(anchor['sprint_id'], anchor['id'])) + 1
    for record in records:
        old = storage.get_artifact(record['artifact_id'])
        if not old:
            raise HTTPException(404, '快照成员已丢失，不能部分回滚')
        name = await asyncio.to_thread(forge.duplicate_deliverable, old.get('file_name') or '')
        if not name:
            raise HTTPException(404, '历史文件已丢失，旧快照未改变')
        regen = copy.deepcopy(old.get('regen') or {})
        regen.update(mode='manual_revision', file_kind=service.kind_of(old), revision_contract=service.snapshot_contract(old),
                     sha256=hashlib.sha256(Path(forge.safe_deliverable_path(name)).read_bytes()).hexdigest(),
                     bundle_pending=True, revision_operation_id=operation_id)
        project = service.project_directory(old)
        if project:
            destination = Path(forge.WORKSPACE_DIR) / 'revisions' / uuid.uuid4().hex / 'project'
            await asyncio.to_thread(shutil.copytree, project, destination)
            regen['project_dir'] = str(destination)
        primary = old['id'] == target['id']
        item = artifacts.add_artifact('final', f'恢复快照 v{version} · {service.kind_of(old)}', old.get('author_roster'),
            old.get('content', ''), file_name=name, parent_id=anchor['id'] if primary else old['id'],
            version=version, regen=regen, quality_status='unchecked', source='local', sprint_id=anchor['sprint_id'])
        record['artifact_id'] = item['id']; changed.append(item)
        if primary:
            restored = item
    if restored is None:
        raise HTTPException(422, '历史快照缺少主版本，未回滚')
    return await _seal(restored, records, changed, f'已恢复 v{target.get("version", 1)} 的整组文件及要求，生成快照 v{version}')
