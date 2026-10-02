from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from al1s.api.editor_channels import browser_connection
from al1s.api.editor_channels import router as channel_router
from al1s.api.editor_ocr import router as ocr_router
from al1s.api.terminal_auth import authenticated_terminal_id
from al1s.app.service_state import service_state
from al1s.execution.editor_service import EditorSessionService
from al1s.execution.editor_sessions import EditorSession, EditorStatus

router = APIRouter()
router.include_router(channel_router)
router.include_router(ocr_router)


class CreateEditor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: UUID


class EditorSummary(BaseModel):
    session_id: UUID
    terminal_id: UUID
    device_id: UUID
    status: EditorStatus
    create_deadline: datetime
    row_version: int
    error_code: str | None


class EditorDetail(EditorSummary):
    connection: dict[str, str] | None = None


class EditorReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: UUID
    status: Literal["active", "closed", "failed"]
    connection: dict[str, Annotated[str, Field(max_length=2048)]] | None = Field(
        default=None, max_length=9
    )


def service(request: Request, response: Response) -> EditorSessionService:
    response.headers["Cache-Control"] = "no-store"
    return service_state(request).editor_sessions


def operator(authorization: str | None) -> None:
    if authorization is not None:
        raise HTTPException(403, "Operator context required")


def summary(item: EditorSession) -> EditorSummary:
    return EditorSummary(
        session_id=item.session_id,
        terminal_id=item.terminal_id,
        device_id=item.device_id,
        status=item.status,
        create_deadline=item.create_deadline,
        row_version=item.row_version,
        error_code=item.error_code,
    )


@router.post("/editor-sessions", response_model=EditorSummary, status_code=201)
def create_editor(
    body: CreateEditor,
    request: Request,
    response: Response,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    authorization: str | None = Header(default=None),
) -> EditorSummary:
    operator(authorization)
    return summary(service(request, response).create(body.device_id, idempotency_key))


@router.get("/editor-sessions/{session_id}", response_model=EditorDetail)
def get_editor(
    session_id: UUID,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
) -> EditorDetail:
    operator(authorization)
    item, connection = service(request, response).detail(session_id)
    return EditorDetail(**summary(item).model_dump(),
                        connection=browser_connection(session_id, connection))


@router.get("/editor-sessions", response_model=EditorSummary | None)
def current_editor(
    device_id: UUID,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
) -> EditorSummary | None:
    operator(authorization)
    item = service(request, response).current(device_id)
    return summary(item) if item else None


@router.delete("/editor-sessions/{session_id}", response_model=EditorSummary)
def close_editor(
    session_id: UUID,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
) -> EditorSummary:
    operator(authorization)
    return summary(service(request, response).close(session_id))


@router.get("/terminal/editor-sessions", response_model=list[EditorSummary])
def terminal_editors(
    request: Request,
    response: Response,
    limit: int = Query(default=20, ge=1, le=50),
    authorization: str | None = Header(default=None),
) -> list[EditorSummary]:
    terminal_id = authenticated_terminal_id(request, authorization)
    return [summary(item) for item in service(request, response).list_terminal(terminal_id, limit)]


@router.post("/terminal/editor-sessions/{session_id}/report", response_model=EditorSummary)
def terminal_report(
    session_id: UUID,
    body: EditorReport,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
) -> EditorSummary:
    terminal_id = authenticated_terminal_id(request, authorization)
    item = service(request, response).report(
        session_id, terminal_id, body.instance_id, EditorStatus(body.status), body.connection
    )
    return summary(item)
