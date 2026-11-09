from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app.auth import require_user
from app.research import gateway, service, store


router = APIRouter(prefix="/api/research", tags=["research"])
MAX_FILE_BYTES = 100 * 1024 * 1024


class ProjectBody(BaseModel):
    name: str
    goal: str = ""
    notes: str = ""


class DocumentReviewBody(BaseModel):
    expected_version: int = Field(ge=1)
    approved: bool = True


class DocumentUpdateBody(BaseModel):
    expected_version: int = Field(ge=1)
    title: str | None = None
    confidentiality: Literal["L1", "L2", "L3"] | None = None


class AnalysisUpdateBody(BaseModel):
    expected_version: int = Field(ge=1)
    analysis: dict[str, Any]
    resolutions: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class SourceHighlightBody(BaseModel):
    expected_version: int = Field(ge=1)
    chunk_id: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=1, max_length=5000)


class ConversationBody(BaseModel):
    title: str = "新会话"


class ChatBody(BaseModel):
    message: str
    conversation_id: str | None = None
    cross_project_ids: list[str] = Field(default_factory=list, max_length=20)
    provider: Literal["local", "rag", "external"] = "local"
    model_id: str = ""
    query_confidentiality: Literal["L1", "L2", "L3"] = "L1"
    task_id: str = ""
    approval_id: str = ""


class OutboundPrepareBody(BaseModel):
    project_id: str
    task_id: str
    document_ids: list[str] = Field(min_length=1, max_length=20)
    provider: str
    model_id: str
    purpose: str = Field(min_length=1, max_length=300)
    operation: Literal["qa", "summary", "embedding", "ocr", "search", "export"]


class OutboundDecisionBody(BaseModel):
    approved: bool


class SourceRequest(BaseModel):
    task_id: str = ""
    approval_id: str = ""
    confidentiality: Literal["L1", "L2", "L3"] = "L1"


class ArxivSearchBody(SourceRequest):
    query: str = Field(min_length=1, max_length=500)


class ArxivImportBody(SourceRequest):
    arxiv_id: str
    version: int = Field(ge=1, le=999)


class GithubImportBody(SourceRequest):
    repository: str
    ref: str = ""
    paths: list[str] = Field(default_factory=list, max_length=5)


class WebpageImportBody(SourceRequest):
    url: str


class GenerationBody(BaseModel):
    kind: Literal["compare", "ablation", "table", "pseudocode", "review", "latex"]
    query: str = ""
    document_ids: list[str] = Field(default_factory=list, max_length=10)


class GenerationUpdateBody(BaseModel):
    content: str = Field(min_length=1, max_length=100000)


class StudyNoteBody(BaseModel):
    title: str
    content: str
    document_type: Literal["baseline", "failure_experiment", "style_sample", "user_result"]
    confidentiality: Literal["L1", "L2", "L3"] = "L2"


class BaselineBody(BaseModel):
    document_id: str


class ExportBody(BaseModel):
    filename: str = ""
    overwrite: bool = False
    confirm_overwrite: bool = False


class ConflictResolutionBody(BaseModel):
    resolution: Literal["overwrite", "discard", "keep_both", "edit"]
    edited_fact: dict[str, Any] | None = None


class BatchBody(BaseModel):
    document_ids: list[str] = Field(min_length=1, max_length=20)
    reanalyze: bool = False


class ConfirmActionBody(BaseModel):
    confirmed: bool = True


class RestoreBackupBody(BaseModel):
    token: str
    sha256: str
    confirmed: bool = False


class DeleteDocumentBody(ConfirmActionBody):
    expected_version: int = Field(ge=1)


class RestoreVersionBody(DeleteDocumentBody):
    target_version: int = Field(ge=1)


def _data(data: Any, status: str = "ok", http_status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, "status": status, "data": data}, status_code=http_status)


async def research_error_handler(_request, exc: service.ResearchError) -> JSONResponse:
    return JSONResponse(
        {"detail": {"code": exc.code, "message": exc.message, **exc.data}},
        status_code=exc.status,
    )


async def policy_error_handler(_request, exc: gateway.PolicyError) -> JSONResponse:
    return JSONResponse(
        {"detail": {"code": exc.code, "message": exc.message, **exc.data}},
        status_code=exc.status,
    )


@router.get("/capabilities")
def capabilities(uid: str = Depends(require_user)):
    from app.gateway.config import get_config

    models = [{
        "id": spec.id, "provider": spec.vendor, "base_url": spec.base_url, "configured": spec.has_key(),
    } for spec in get_config().models.values()]
    from app.research.local_components import component_status

    return _data({
        "mode": "research-p4",
        "local": {
            "document_parsers": ["markdown", "pdf", "docx", "tex", "csv", "html"],
            "retrieval": "local-lexical+bge-m3",
            "question_answering": "local-extractive+local-rag",
            **component_status(),
        },
        "external_models": models,
        "automatic_memory_write": False,
        "automatic_summary": False,
        "automatic_model_fallback": False,
        "user_id": uid,
    })


@router.get("/projects")
def projects(uid: str = Depends(require_user)):
    return _data({"items": service.list_projects(uid)})


@router.post("/projects")
def create_project(body: ProjectBody, uid: str = Depends(require_user)):
    return _data(service.create_project(uid, body.name, body.goal, body.notes), "created", 201)


@router.get("/projects/{project_id}")
def project_detail(project_id: str, uid: str = Depends(require_user)):
    items = service.list_projects(uid)
    project = next((item for item in items if item["id"] == project_id), None)
    if project is None:
        raise service.ResearchError(404, "project_not_found", "课题不存在或不属于当前用户")
    return _data(project)


@router.post("/projects/{project_id}/documents")
async def import_document(
    project_id: str,
    file: UploadFile = File(...),
    confidentiality: Literal["L1", "L2", "L3"] = Form("L2"),
    document_type: Literal["tutorial", "paper", "repository"] | None = Form(None),
    chunking_strategy: Literal["auto", "parent_recursive", "recursive", "fixed", "semantic"] = Form("auto"),
    uid: str = Depends(require_user),
):
    content = await file.read(MAX_FILE_BYTES + 1)
    await file.close()
    item = service.import_document(
        uid, project_id, filename=file.filename or "", content=content,
        confidentiality=confidentiality, document_type=document_type,
        chunking_strategy=chunking_strategy,
    )
    return _data(item, "pending_review", 201)


@router.get("/projects/{project_id}/documents")
def documents(project_id: str, uid: str = Depends(require_user)):
    return _data({"items": service.list_documents(uid, project_id)})


@router.get("/projects/{project_id}/memories")
def memories(project_id: str, uid: str = Depends(require_user)):
    return _data({"items": service.list_memories(uid, project_id)})


@router.get("/documents/{document_id}")
def document(document_id: str, uid: str = Depends(require_user)):
    return _data(service.get_document(uid, document_id))


@router.get("/documents/{document_id}/source-file")
def source_file(document_id: str, uid: str = Depends(require_user)):
    from pathlib import Path

    from app.data_access.research import migration_session

    with migration_session() as conn:
        row = service._document(conn, uid, document_id)
        path = Path(row.stored_path)
        filename = row.original_filename
    if not path.is_file():
        raise service.ResearchError(404, "source_file_missing", "系统副本不存在")
    return FileResponse(path, filename=filename, content_disposition_type="inline")


@router.post("/documents/{document_id}/highlight")
def highlight_source(document_id: str, body: SourceHighlightBody, uid: str = Depends(require_user)):
    """Render an exact, unambiguous quote on its source page without changing the PDF."""
    import hashlib
    from pathlib import Path
    from sqlalchemy import select
    from app.data_access.research import migration_session

    with migration_session() as conn:
        row = service._document(conn, uid, document_id)
        if row.version != body.expected_version:
            raise service.ResearchError(409, "document_version_changed", "资料版本已变化，请刷新后重试")
        chunk = conn.execute(select(store.ResearchChunk).where(
            store.ResearchChunk.id == body.chunk_id,
            store.ResearchChunk.user_id == uid,
            store.ResearchChunk.document_id == document_id,
            store.ResearchChunk.document_version == row.version,
        )).scalar_one_or_none()
        if not chunk or body.quote not in chunk.content:
            raise service.ResearchError(422, "highlight_unavailable", "引文不在该来源片段中，无法可靠定位")
        locator = store.json_value(chunk.locator_json, {})
        page_number = locator.get("page")
        path = Path(row.stored_path)
        if path.suffix.lower() != ".pdf" or not isinstance(page_number, int):
            raise service.ResearchError(422, "highlight_unavailable", "该来源没有可定位的 PDF 页面")
        if not path.is_file():
            raise service.ResearchError(404, "source_file_missing", "系统副本不存在")
        with path.open("rb") as source_file:
            digest = hashlib.file_digest(source_file, "sha256").hexdigest()
        if digest != row.sha256:
            raise service.ResearchError(409, "source_file_changed", "系统副本与原资料不一致，请重新导入")
        try:
            import fitz
            with fitz.open(path) as pdf:
                if page_number < 1 or page_number > len(pdf):
                    raise service.ResearchError(422, "highlight_unavailable", "来源页码无效")
                page = pdf[page_number - 1]
                from app.research.highlight import page_rectangles
                rectangles = page_rectangles(page, body.quote, locator)
                if not rectangles:
                    raise service.ResearchError(422, "highlight_unavailable", "原文无法唯一定位；旧扫描件请重新导入并识别，或人工核对")
                page.add_highlight_annot(rectangles)
                scale = min(1.5, 1800 / max(page.rect.width, page.rect.height))
                image = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
        except ImportError as exc:
            raise service.ResearchError(503, "highlight_unavailable", "本地 PDF 渲染组件不可用") from exc
    return Response(image, media_type="image/png", headers={"Cache-Control": "no-store", "X-Source-Page": str(page_number), "X-Source-Extraction": locator.get("extraction", "")})


@router.post("/documents/{document_id}/ocr")
def run_ocr(document_id: str, uid: str = Depends(require_user)):
    from app.research.ocr import ocr_document

    return _data(ocr_document(uid, document_id), "ocr_completed")


@router.post("/documents/{document_id}/analysis")
def analyze_document(document_id: str, uid: str = Depends(require_user)):
    from app.research.paper_analysis import analyze_document as analyze

    result = analyze(uid, document_id)
    return _data(result, result["analysis_status"])


@router.put("/documents/{document_id}/analysis")
def edit_analysis(document_id: str, body: AnalysisUpdateBody, uid: str = Depends(require_user)):
    from app.research.paper_analysis import update_analysis

    return _data(update_analysis(
        uid, document_id, expected_version=body.expected_version, payload=body.analysis, resolutions=body.resolutions,
    ), "analysis_ready")


@router.patch("/documents/{document_id}")
def update_document(document_id: str, body: DocumentUpdateBody, uid: str = Depends(require_user)):
    item = service.update_document(
        uid, document_id, expected_version=body.expected_version,
        title=body.title, confidentiality=body.confidentiality,
    )
    return _data(item, "pending_review")


@router.post("/documents/{document_id}/review")
def review_document(document_id: str, body: DocumentReviewBody, uid: str = Depends(require_user)):
    item = service.review_document(
        uid, document_id, expected_version=body.expected_version, approved=body.approved
    )
    return _data(item, "approved" if body.approved else "rejected")


@router.post("/projects/{project_id}/conversations")
def new_conversation(project_id: str, body: ConversationBody | None = None, uid: str = Depends(require_user)):
    title = body.title if body else "新会话"
    return _data(service.create_conversation(uid, project_id, title), "created", 201)


@router.get("/projects/{project_id}/conversations")
def conversations(project_id: str, uid: str = Depends(require_user)):
    return _data({"items": service.list_conversations(uid, project_id)})


@router.get("/conversations/{conversation_id}/messages")
def messages(conversation_id: str, uid: str = Depends(require_user)):
    return _data({"items": service.conversation_messages(uid, conversation_id)})


@router.post("/projects/{project_id}/chat")
def chat(project_id: str, body: ChatBody, uid: str = Depends(require_user)):
    result = service.chat(
        uid, project_id, message=body.message, conversation_id=body.conversation_id,
        cross_project_ids=body.cross_project_ids, provider=body.provider, model_id=body.model_id,
        query_confidentiality=body.query_confidentiality, task_id=body.task_id, approval_id=body.approval_id,
    )
    return _data(result, result["status"])


@router.post("/projects/{project_id}/study-notes")
def create_study_note(project_id: str, body: StudyNoteBody, uid: str = Depends(require_user)):
    from app.research.generations import create_study_note as create

    return _data(create(
        uid, project_id, title=body.title, content=body.content,
        document_type=body.document_type, confidentiality=body.confidentiality,
    ), "pending_review", 201)


@router.post("/projects/{project_id}/baseline")
def set_baseline(project_id: str, body: BaselineBody, uid: str = Depends(require_user)):
    from app.research.generations import set_baseline as set_current

    return _data(set_current(uid, project_id, body.document_id), "baseline_set")


@router.post("/projects/{project_id}/generations")
def generate(project_id: str, body: GenerationBody, uid: str = Depends(require_user)):
    from app.research.generations import generate_output

    return _data(generate_output(
        uid, project_id, kind=body.kind, query=body.query, document_ids=body.document_ids,
    ), "generated", 201)


@router.get("/generations/{generation_id}")
def generation_detail(generation_id: str, uid: str = Depends(require_user)):
    from app.research.generations import _generation_public, get_generation

    return _data(_generation_public(get_generation(uid, generation_id)))


@router.put("/generations/{generation_id}")
def update_generation(generation_id: str, body: GenerationUpdateBody, uid: str = Depends(require_user)):
    from app.research.generations import update_generation as update

    return _data(update(uid, generation_id, content=body.content), "pending_review")


@router.get("/generations/{generation_id}/download")
def download_generation(generation_id: str, filename: str = "", uid: str = Depends(require_user)):
    from pathlib import Path

    from app.research.generations import get_generation

    row = get_generation(uid, generation_id)
    if row.status == "rejected":
        raise service.ResearchError(409, "generation_requires_correction", "拒绝稿须修正后才能下载")
    suffix = ".tex" if row.kind == "latex" else ".md"
    safe_name = Path(filename or f"{row.kind}{suffix}").name
    directory = (store.data_dir() / "exports" / f"{uid}/{row.project_id}").resolve()
    target = (directory / safe_name).resolve()
    if target.parent != directory or not target.is_file():
        raise service.ResearchError(404, "exported_file_missing", "导出文件不存在")
    return FileResponse(target, filename=target.name)


@router.post("/generations/{generation_id}/archive")
def archive_generation(generation_id: str, uid: str = Depends(require_user)):
    from app.research.generations import archive_generation as archive

    return _data(archive(uid, generation_id), "pending_review", 201)


@router.post("/generations/{generation_id}/export")
def export_generation(generation_id: str, body: ExportBody, uid: str = Depends(require_user)):
    from app.research.generations import export_generation as export

    return _data(export(
        uid, generation_id, filename=body.filename,
        overwrite=body.overwrite, confirm_overwrite=body.confirm_overwrite,
    ), "exported", 201)


@router.get("/projects/{project_id}/conflicts")
def conflicts(project_id: str, uid: str = Depends(require_user)):
    from app.research.conflicts import list_conflicts

    return _data({"items": list_conflicts(uid, project_id)})


@router.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: str, body: ConflictResolutionBody, uid: str = Depends(require_user)):
    from app.research.conflicts import resolve_conflict as resolve

    return _data(resolve(
        uid, conflict_id, resolution=body.resolution, edited_fact=body.edited_fact,
    ), "conflict_resolved")


@router.post("/projects/{project_id}/batches")
def create_batch(project_id: str, body: BatchBody, uid: str = Depends(require_user)):
    from app.research.batches import create_batch as create

    return _data(create(uid, project_id, body.document_ids, operation="reanalyze" if body.reanalyze else "analysis"), "queued", 202)


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: str, uid: str = Depends(require_user)):
    from app.research.batches import get_batch

    return _data(get_batch(uid, batch_id))


@router.get("/tasks")
def tasks(uid: str = Depends(require_user)):
    from app.research.batches import list_tasks
    from app.research.async_tasks import list_tasks as list_async
    return _data({"items": list_tasks(uid), "async_items": list_async(uid)})


@router.post("/projects/{project_id}/chat-tasks")
def create_chat_task(project_id: str, body: ChatBody, uid: str = Depends(require_user)):
    from app.research.async_tasks import create
    return _data(create(uid, project_id, "chat", body.model_dump()), "queued", 202)


@router.post("/projects/{project_id}/generation-tasks")
def create_generation_task(project_id: str, body: GenerationBody, uid: str = Depends(require_user)):
    from app.research.async_tasks import create
    return _data(create(uid, project_id, "generation", body.model_dump()), "queued", 202)


@router.post("/projects/{project_id}/document-tasks")
async def create_document_task(project_id: str, file: UploadFile = File(...),
    confidentiality: Literal["L1", "L2", "L3"] = Form("L2"),
    document_type: Literal["tutorial", "paper", "repository"] | None = Form(None),
    chunking_strategy: Literal["auto", "parent_recursive", "recursive", "fixed", "semantic"] = Form("auto"),
    uid: str = Depends(require_user)):
    from app.research.async_tasks import create, stage
    from app.data_access.research import migration_session
    with migration_session() as conn:
        service._project(conn, uid, project_id)
    content = await file.read(MAX_FILE_BYTES + 1)
    await file.close()
    staged = stage(uid, content, MAX_FILE_BYTES)
    return _data(create(uid, project_id, 'upload', {**staged, 'filename': file.filename or '',
        'confidentiality': confidentiality, 'document_type': document_type, 'chunking_strategy': chunking_strategy}), 'queued', 202)


def _source_task(uid, project_id, kind, body):
    from app.research.async_tasks import create
    values = body.model_dump(exclude={'task_id', 'approval_id', 'confidentiality'})
    values['level'] = body.confidentiality
    return _data(create(uid, project_id, kind, values), 'queued', 202)


@router.post("/projects/{project_id}/source-tasks/arxiv/search")
def create_arxiv_search_task(project_id: str, body: ArxivSearchBody, uid: str = Depends(require_user)):
    return _source_task(uid, project_id, 'arxiv_search', body)


@router.post("/projects/{project_id}/source-tasks/arxiv/import")
def create_arxiv_import_task(project_id: str, body: ArxivImportBody, uid: str = Depends(require_user)):
    return _source_task(uid, project_id, 'arxiv_import', body)


@router.post("/projects/{project_id}/source-tasks/github/import")
def create_github_task(project_id: str, body: GithubImportBody, uid: str = Depends(require_user)):
    return _source_task(uid, project_id, 'github_import', body)


@router.post("/projects/{project_id}/source-tasks/webpage/import")
def create_webpage_task(project_id: str, body: WebpageImportBody, uid: str = Depends(require_user)):
    return _source_task(uid, project_id, 'webpage_import', body)


@router.post("/backup-tasks/{backup_kind}")
def create_backup_task(backup_kind: Literal['full', 'light'], body: ConfirmActionBody, uid: str = Depends(require_user)):
    if not body.confirmed:
        raise service.ResearchError(409, 'export_confirmation_required', '请确认创建备份')
    from app.research.async_tasks import create
    return _data(create(uid, '', 'backup_' + backup_kind, {}), 'queued', 202)


@router.post("/restore-tasks/preview")
async def create_restore_preview_task(file: UploadFile = File(...), uid: str = Depends(require_user)):
    from app.research.async_tasks import create, stage
    from app.research.restore_backup import MAX_ARCHIVE
    content = await file.read(MAX_ARCHIVE + 1)
    await file.close()
    return _data(create(uid, '', 'restore_preview', stage(uid, content, MAX_ARCHIVE)), 'queued', 202)


@router.post("/restore-tasks/confirm")
def create_restore_confirm_task(body: RestoreBackupBody, uid: str = Depends(require_user)):
    if not body.confirmed:
        raise service.ResearchError(409, 'restore_confirmation_required', '请先确认预览后再恢复')
    from app.research.async_tasks import create
    return _data(create(uid, '', 'restore_confirm', {'token': body.token, 'expected_sha256': body.sha256, 'confirmed': True}), 'queued', 202)


@router.get("/async-tasks/{task_id}")
def async_task(task_id: str, uid: str = Depends(require_user)):
    from app.research.async_tasks import get_task
    return _data(get_task(uid, task_id))


@router.post("/async-tasks/{task_id}/cancel")
def cancel_async_task(task_id: str, uid: str = Depends(require_user)):
    from app.research.async_tasks import cancel
    return _data(cancel(uid, task_id))


@router.post("/async-tasks/{task_id}/resume")
def resume_async_task(task_id: str, uid: str = Depends(require_user)):
    from app.research.async_tasks import resume
    return _data(resume(uid, task_id), "queued", 202)


@router.post("/async-tasks/{task_id}/retry")
def retry_async_task(task_id: str, uid: str = Depends(require_user)):
    from app.research.async_tasks import retry
    return _data(retry(uid, task_id), "queued", 202)


@router.post("/batch-items/{item_id}/cancel")
def cancel_batch_item(item_id: str, uid: str = Depends(require_user)):
    from app.research.batches import cancel_batch_item as cancel
    return _data(cancel(uid, item_id), "stop_requested")


@router.post("/batch-items/{item_id}/retry")
def retry_batch_item(item_id: str, uid: str = Depends(require_user)):
    from app.research.batches import retry_batch_item as retry

    return _data(retry(uid, item_id), "queued", 202)


@router.get("/documents/{document_id}/versions")
def document_versions(document_id: str, uid: str = Depends(require_user)):
    from app.research.lifecycle import list_versions

    return _data({"items": list_versions(uid, document_id)})


@router.post("/documents/{document_id}/restore-version")
def restore_version(document_id: str, body: RestoreVersionBody, uid: str = Depends(require_user)):
    from app.research.lifecycle import restore_version as restore

    return _data(restore(
        uid, document_id, target_version=body.target_version,
        expected_version=body.expected_version, confirmed=body.confirmed,
    ), "restored")


@router.post("/documents/{document_id}/delete")
def delete_document(document_id: str, body: DeleteDocumentBody, uid: str = Depends(require_user)):
    from app.research.lifecycle import delete_document as delete

    return _data(delete(uid, document_id, expected_version=body.expected_version, confirmed=body.confirmed), "deleted")


@router.post("/documents/{document_id}/restore")
def restore_document(document_id: str, body: ConfirmActionBody, uid: str = Depends(require_user)):
    from app.research.lifecycle import restore_document as restore

    return _data(restore(uid, document_id, confirmed=body.confirmed), "restored")


@router.delete("/documents/{document_id}/permanent")
def permanent_delete_document(document_id: str, body: ConfirmActionBody, uid: str = Depends(require_user)):
    from app.research.lifecycle import permanent_delete_document as permanent_delete

    return _data(permanent_delete(uid, document_id, confirmed=body.confirmed), "permanently_deleted")


@router.get("/projects/{project_id}/recycle-bin")
def recycle_bin(project_id: str, uid: str = Depends(require_user)):
    from app.research.lifecycle import list_recycle_bin

    return _data({"items": list_recycle_bin(uid, project_id)})


@router.get("/projects/{project_id}/changes")
def changes(project_id: str, limit: int = Query(10, ge=1, le=10), uid: str = Depends(require_user)):
    from app.research.lifecycle import list_changes

    return _data({"items": list_changes(uid, project_id, limit=limit)})


@router.post("/changes/{change_id}/undo")
def undo_change(change_id: str, body: ConfirmActionBody, uid: str = Depends(require_user)):
    from app.research.lifecycle import undo_change as undo

    return _data(undo(uid, change_id, confirmed=body.confirmed), "undone")


@router.post("/export")
def export_library(body: ConfirmActionBody, uid: str = Depends(require_user)):
    from app.research.backup import export_library as export

    return _data(export(uid, confirmed=body.confirmed), "exported", 201)


@router.post("/export/light")
def export_lightweight(body: ConfirmActionBody, uid: str = Depends(require_user)):
    from app.research.backup import export_lightweight as export

    return _data(export(uid, confirmed=body.confirmed), "exported", 201)


@router.get("/export/full/download")
def download_full_backup(filename: str, uid: str = Depends(require_user)):
    from pathlib import Path
    if Path(filename).name != filename or not filename.startswith("research-starship-backup") or not filename.endswith(".zip"):
        raise service.ResearchError(404, "backup_file_missing", "备份文件不存在")
    target = store.data_dir() / "exports" / uid / filename
    if not target.is_file():
        raise service.ResearchError(404, "backup_file_missing", "备份文件不存在")
    return FileResponse(target, filename=filename, media_type="application/zip")


@router.post("/restore/preview")
async def preview_backup(file: UploadFile = File(...), uid: str = Depends(require_user)):
    from app.research.restore_backup import MAX_ARCHIVE, preview
    content = await file.read(MAX_ARCHIVE + 1)
    return _data(preview(uid, content), "previewed")


@router.post("/restore/confirm")
def confirm_backup_restore(body: RestoreBackupBody, uid: str = Depends(require_user)):
    from app.research.restore_backup import restore
    return _data(restore(uid, body.token, body.sha256, body.confirmed), "restored", 201)


@router.get("/export/light/download")
def download_lightweight(filename: str, uid: str = Depends(require_user)):
    from pathlib import Path

    safe_name = Path(filename).name
    directory = (store.data_dir() / "exports" / uid).resolve()
    target = (directory / safe_name).resolve()
    if target.parent != directory or not target.is_file():
        raise service.ResearchError(404, "backup_file_missing", "备份文件不存在")
    return FileResponse(target, filename=target.name)


@router.post("/projects/{project_id}/sources/arxiv/search")
def arxiv_search(project_id: str, body: ArxivSearchBody, uid: str = Depends(require_user)):
    from app.research.external_sources import search_arxiv

    result = search_arxiv(
        uid, project_id, query=body.query, level=body.confidentiality,
        task_id=body.task_id, approval_id=body.approval_id,
    )
    return _data(result, result["status"])


@router.post("/projects/{project_id}/sources/arxiv/import")
def arxiv_import(project_id: str, body: ArxivImportBody, uid: str = Depends(require_user)):
    from app.research.external_sources import import_arxiv_paper

    result = import_arxiv_paper(
        uid, project_id, arxiv_id=body.arxiv_id, version=body.version,
        level=body.confidentiality, task_id=body.task_id, approval_id=body.approval_id,
    )
    return _data(result, result["status"], 201 if result["status"] == "pending_review" else 200)


@router.post("/projects/{project_id}/sources/github/import")
def github_import(project_id: str, body: GithubImportBody, uid: str = Depends(require_user)):
    from app.research.external_sources import import_github_repository

    result = import_github_repository(
        uid, project_id, repository=body.repository, ref=body.ref, paths=body.paths,
        level=body.confidentiality, task_id=body.task_id, approval_id=body.approval_id,
    )
    return _data(result, result["status"], 201 if result["status"] == "pending_review" else 200)


@router.post("/projects/{project_id}/sources/webpage/import")
def webpage_import(project_id: str, body: WebpageImportBody, uid: str = Depends(require_user)):
    from app.crawl.errors import CrawlError
    from app.research.external_sources import import_webpage

    try:
        result = import_webpage(
            uid, project_id, url=body.url, level=body.confidentiality,
            task_id=body.task_id, approval_id=body.approval_id,
        )
    except CrawlError as exc:
        raise service.ResearchError(400, "webpage_url_forbidden", str(exc)) from exc
    except ValueError as exc:
        raise service.ResearchError(400, "webpage_request_invalid", str(exc)) from exc
    return _data(result, result["status"], 201 if result["status"] == "pending_review" else 200)


@router.post("/outbound/prepare")
def prepare_outbound(body: OutboundPrepareBody, uid: str = Depends(require_user)):
    approval = service.prepare_documents_outbound(
        uid, body.project_id, task_id=body.task_id,
        document_ids=body.document_ids, provider=body.provider, model_id=body.model_id,
        purpose=body.purpose, operation=body.operation,
    )
    return _data(approval, approval["status"])


@router.get("/outbound/{approval_id}")
def outbound_detail(approval_id: str, uid: str = Depends(require_user)):
    return _data(gateway.get(uid, approval_id))


@router.post("/outbound/{approval_id}/decision")
def outbound_decision(approval_id: str, body: OutboundDecisionBody, uid: str = Depends(require_user)):
    return _data(gateway.decide(uid, approval_id, body.approved), "approved" if body.approved else "rejected")


@router.post("/outbound/{approval_id}/revoke")
def outbound_revoke(approval_id: str, uid: str = Depends(require_user)):
    return _data(gateway.revoke(uid, approval_id), "revoked")


@router.get("/projects/{project_id}/security-events")
def events(project_id: str, limit: int = Query(100, ge=1, le=500), uid: str = Depends(require_user)):
    return _data({"items": service.security_events(uid, project_id, limit)})


store.init_db()
