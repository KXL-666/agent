"""多格式成品引擎入口：对外暴露生成、预览、自检与代码产物的统一接口。

具体实现按职责拆分：
- deliverable_builder   解析与确定性渲染（docx / pptx / xlsx）
- deliverable_generator 内容生成、清洗与有限修订
- artifact_preview      预览、自检与 Markdown 重渲染
- artifact_code         代码类产物与项目整合

本模块继续持有产物目录（WORKSPACE_DIR / DELIVERABLES_DIR）与当前代码目录（CODE_DIR），
运行时可被测试或部署覆盖，拆分后的模块都读取这里的当前值。
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DELIVERABLES_DIR = os.path.join(BASE_DIR, "deliverables")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
CODE_ROOT = os.path.join(WORKSPACE_DIR, "code")
# 当前 Sprint 的代码目录（每轮独立，避免新任务覆盖历史代码）
CODE_DIR = CODE_ROOT

os.makedirs(DELIVERABLES_DIR, exist_ok=True)
os.makedirs(CODE_DIR, exist_ok=True)

# graph / routers 通过 forge.semantic_filename 取文件名
from artifact_naming import semantic_filename  # noqa: E402,F401

# ---------- 路径与保存位置（实现见 path_manager） ----------

from path_manager import (  # noqa: E402
    _unique_name,
    copy_to_target,
    duplicate_deliverable,
    extract_target_dir,
    list_known_locations,
    output_path,
    publish_file,
    safe_deliverable_path,
    set_explicit_save_dir,
)

# ---------- 内容解析与确定性渲染（实现见 deliverable_builder） ----------

from deliverable_builder import (  # noqa: E402
    CONTENT_KINDS,
    _bold_segments,
    _build_docx,
    _build_pptx,
    _build_xlsx,
    _parse_docx_markdown,
    _sanitize_pptx,
    _sanitize_xlsx,
)

_CONTENT_KINDS = CONTENT_KINDS

# ---------- 内容生成与有限修订（实现见 deliverable_generator） ----------

from deliverable_generator import (  # noqa: E402
    _docx_content_prompt,
    _docx_prose,
    _fit_docx_length,
    _forge_once,
    _gen_docx,
    _gen_pptx,
    _gen_xlsx,
    _reconcile_pending_claims,
    _strip_unrequested_docx_placeholders,
    forge_file,
)

# ---------- 预览与自检（实现见 artifact_preview） ----------

from artifact_preview import (  # noqa: E402
    extract_preview,
    pdf_from_docx,
    preview_html,
    render_docx_from_markdown,
    render_document_from_markdown,
    self_check,
    self_check_note,
)

# ---------- 代码类产物（实现见 artifact_code） ----------

from artifact_code import (  # noqa: E402
    _parse_code_files,
    begin_sprint,
    create_project,
    finalize_project,
    forge_code,
    project_report,
    selected_project,
    sprint_code_dir,
    verify_generated_code,
)
