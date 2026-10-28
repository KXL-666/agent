"""Version-isolated project planning and multi-file revisions."""
import ast
import json
import re
import shutil
import uuid
import zipfile
from pathlib import Path

from fastapi import HTTPException

import artifact_quality
import forge
import llm
import project_agent


def is_test(name: str) -> bool:
    path = Path(name)
    return ('tests' in path.parts or '__tests__' in path.parts or path.name.startswith('test_')
            or path.stem in ('test', 'tests') or path.stem.endswith('Test') or path.stem.endswith('Tests')
            or bool(re.search(r'[._](test|spec)[._]', path.name)))


def assertions(path: str, text: str) -> set[str]:
    if path.endswith('.py'):
        try:
            tree = ast.parse(text)
            return {ast.dump(node, include_attributes=False) for node in ast.walk(tree)
                    if isinstance(node, ast.Assert) or isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr.startswith('assert')}
        except SyntaxError:
            return set()
    return {re.sub(r'\s+', '', line) for line in text.splitlines() if re.search(r'assert\b|expect\(', line)}


def source_snapshot(root: Path, limit: int = 120000) -> str:
    blocks = []
    for path in project_agent.project_files(root):
        if path.name == '.project-quality.json':
            continue
        data = path.read_bytes()
        if b'\x00' in data:
            continue
        text = data.decode('utf-8', errors='strict')
        blocks.append(f'===FILE: {path.relative_to(root).as_posix()}\n{text}\n===END')
        if sum(map(len, blocks)) > limit:
            raise HTTPException(422, '项目超过本轮完整编辑上下文，请先拆分项目；未截断源码进行重构')
    return '\n'.join(blocks)


def build(requirement: str, source: str | None = None) -> dict:
    identity = uuid.uuid4().hex
    root = Path(forge.WORKSPACE_DIR) / 'revisions' / identity / 'project'
    root.mkdir(parents=True, exist_ok=True)
    original = Path(source) if source else None
    existing = source_snapshot(original) if original else ''
    if original:
        for path in project_agent.project_files(original):
            if path.name == '.project-quality.json':
                continue
            target = project_agent.safe_path(root, path.relative_to(original).as_posix())
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    meta = {}
    plan = llm.chat_json(
        '你是软件项目负责人。按需求规划本轮新增、修改、删除的文件及共享接口，保持未要求修改的功能。'
        '输出 JSON {"runtime":"python|node|unsupported","summary":"改造方案",'
        '"files":[{"path":"相对路径","action":"write|delete","purpose":"职责及接口"}]}。'
        '最多40个文件，必须有可运行入口、依赖清单、README和带真实断言的测试。'
        '已有测试不得删除、跳过或弱化；需要补充覆盖时新增测试。跨文件接口、导入和依赖必须一致。'
        '尊重用户指定栈，未支持语言标为unsupported，不得伪造运行验证。',
        json.dumps({'requirement': requirement, 'current_project': existing}, ensure_ascii=False),
        fallback={}, meta=meta, roster=2, max_tokens=6000, timeout=180,
    )
    if meta.get('fallback_reason') or not isinstance(plan, dict) or not isinstance(plan.get('files'), list):
        raise HTTPException(502, '未生成有效的多文件改造计划')
    files = plan['files']
    if not 1 <= len(files) <= project_agent.MAX_FILES:
        raise HTTPException(422, '改造计划文件数量无效')
    paths, writes = set(), []
    for item in files:
        if not isinstance(item, dict) or item.get('action') not in ('write', 'delete'):
            raise HTTPException(422, '改造计划包含无效文件操作')
        name = str(item.get('path', '')).replace('\\', '/')
        try:
            target = project_agent.safe_path(root, name)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if name.casefold() in paths:
            raise HTTPException(422, '改造计划包含重复或大小写冲突的文件')
        paths.add(name.casefold())
        if item['action'] == 'delete':
            if is_test(name):
                raise HTTPException(422, '不能通过删除测试完成重构')
            if not target.is_file():
                raise HTTPException(422, f'计划删除的文件不存在：{name}')
            target.unlink()
        else:
            writes.append(name)
    for offset in range(0, len(writes), 4):
        batch = writes[offset:offset + 4]
        meta = {}
        raw = llm.chat_text(
            '实现计划中的文件。只输出 ===FILE: 相对路径\n完整内容\n===END。'
            '不得输出未在write_only中的文件。保持共享接口一致，不删除或弱化既有测试断言。'
            '实现真实功能、错误处理和测试，不写TODO、跳过测试或已验证的自述。',
            [{'role': 'user', 'content': json.dumps({'requirement': requirement, 'plan': plan,
              'write_only': batch, 'current_project': source_snapshot(root)}, ensure_ascii=False)}],
            fallback='', roster=2, meta=meta, max_tokens=16000, timeout=300,
        )
        if meta.get('fallback_reason'):
            raise HTTPException(502, '多文件生成中断，原版本未改动')
        written = project_agent.write_response(root, raw, set(batch))
        if set(written) != set(batch):
            raise HTTPException(422, '模型未完整返回计划文件：' + '、'.join(sorted(set(batch) - set(written))))
    if original:
        for path in project_agent.project_files(original):
            name = path.relative_to(original).as_posix()
            if is_test(name):
                updated = project_agent.safe_path(root, name)
                if not updated.is_file() or not assertions(name, path.read_text(encoding='utf-8')) <= assertions(name, updated.read_text(encoding='utf-8')):
                    raise HTTPException(422, f'本轮改造移除或弱化了既有测试：{name}')
    runtime = plan.get('runtime')
    if runtime not in ('python', 'node', 'unsupported'):
        raise HTTPException(422, '项目运行时无效')
    all_paths = [path.relative_to(root).as_posix() for path in project_agent.project_files(root)]
    if not any(is_test(name) for name in all_paths) or 'README.md' not in all_paths:
        raise HTTPException(422, '项目缺少README或实际测试文件')
    report = project_agent.evaluate(root, all_paths, runtime)
    report.update(runtime=runtime, planned_files=all_paths, requirement=requirement, revision_plan=plan)
    (root / '.project-quality.json').write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    name = f'_process/revisions/{identity}/项目.zip'
    with zipfile.ZipFile(forge.output_path(name), 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in project_agent.project_files(root):
            if path.name != '.project-quality.json':
                archive.write(path, path.relative_to(root).as_posix())
    artifact_quality.save_report(forge.safe_deliverable_path(name), forge.WORKSPACE_DIR, report)
    return {'file_name': name, 'source': 'model', 'summary': str(plan.get('summary') or '已完成多文件改造'),
            'regen': {'project_dir': str(root), 'runtime': runtime, 'planned_files': all_paths,
                      'revision_plan': plan}}
