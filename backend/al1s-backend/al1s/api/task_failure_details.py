import re
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from al1s.app.service_state import service_state
from al1s.execution.failure_details import FailureDetail
from al1s.execution.failure_review_service import FailureReviewService
from al1s.execution.task_captures import TaskCapture

router = APIRouter()


class AttemptDetailsResponse(BaseModel):
    attempt_id: UUID
    execution_id: UUID
    attempt_no: int
    status: str
    result: str | None
    error_code: str | None
    failure_phase: str | None
    confirmed: bool
    expired: bool
    no_screenshot: bool
    details: list[FailureDetail]
    screenshots: list[TaskCapture] = Field(default_factory=list)


class TaskDetailsResponse(BaseModel):
    source_module: str | None = None
    items: list[AttemptDetailsResponse]
    next_cursor: UUID | None
    lineup_record_id: UUID | None = None


def _service(request: Request, authorization: str | None) -> FailureReviewService:
    if authorization is not None:
        raise HTTPException(403, "Operator context required")
    return service_state(request).failure_reviews


@router.get("/{task_id}/details")
def details(
    task_id: UUID,
    request: Request,
    response: Response,
    cursor: UUID | None = None,
    authorization: str | None = Header(default=None),
) -> TaskDetailsResponse:
    response.headers["Cache-Control"] = "no-store"
    return TaskDetailsResponse.model_validate(
        _service(request, authorization).details(task_id, cursor)
    )


@router.get("/{task_id}/attempts/{attempt_id}/screenshots/{artifact_id}")
def screenshot(
    task_id: UUID,
    attempt_id: UUID,
    artifact_id: UUID,
    request: Request,
    preview: bool = False,
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    service = _service(request, authorization)
    item = service.metadata(task_id, attempt_id, artifact_id)
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", item.file_name)[:200]
    return StreamingResponse(
        service.stream(task_id, attempt_id, artifact_id, item, preview=preview),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": (
                f"attachment; filename=screenshot; filename*=UTF-8''{quote(name, safe='')}"
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(item.size_bytes),
            "X-Content-SHA256": item.sha256,
        },
    )


@router.post("/{task_id}/attempts/{attempt_id}/confirm-failure", status_code=204)
def confirm(
    task_id: UUID,
    attempt_id: UUID,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    _service(request, authorization).confirm(task_id, attempt_id)
    return Response(status_code=204)
