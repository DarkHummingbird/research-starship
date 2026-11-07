from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse
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


class ConversationBody(BaseModel):
    title: str = "新会话"


class ChatBody(BaseModel):
    message: str
    conversation_id: str | None = None
    cross_project_ids: list[str] = Field(default_factory=list, max_length=20)
    provider: Literal["local", "external"] = "local"
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
    return _data({
        "mode": "research-p1",
        "local": {
            "markdown_parser": "available",
            "retrieval": "local-lexical-v1",
            "question_answering": "local-extractive-v1",
            "ocr": "not_integrated",
            "neural_embedding": "not_integrated",
            "generative_llm": "not_integrated",
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
    uid: str = Depends(require_user),
):
    content = await file.read(MAX_FILE_BYTES + 1)
    await file.close()
    item = service.import_markdown(
        uid, project_id, filename=file.filename or "", content=content, confidentiality=confidentiality
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
