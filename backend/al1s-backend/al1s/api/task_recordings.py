import re
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from al1s.app.service_state import service_state
from al1s.execution.errors import InvalidRequestError
from al1s.execution.recording_download import RecordingDownloadService, RecordingFile, RecordingPage

router = APIRouter()
_RANGE = re.compile(r"bytes=(0|[1-9][0-9]*)-(0|[1-9][0-9]*)\Z")


def _service(request: Request, authorization: str | None) -> RecordingDownloadService:
    # Operator endpoints currently run in the trusted LAN; a terminal bearer
    # credential must never become an operator download capability.
    if authorization is not None:
        raise HTTPException(status_code=403, detail="Operator context required")
    return service_state(request).recording_downloads


def _headers(item: RecordingFile) -> dict[str, str]:
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", item.file_name)[:200]
    return {
        "Content-Type": "video/mp4",
        "Content-Disposition": (
            f"attachment; filename=recording.mp4; filename*=UTF-8''{quote(name, safe='')}"
        ),
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Content-SHA256": item.sha256,
        "ETag": f'"sha256:{item.sha256}"',
    }


@router.head("/{task_id}/recordings/{artifact_id}")
def recording_metadata(
    task_id: UUID,
    artifact_id: UUID,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    item = _service(request, authorization).metadata(task_id, artifact_id)
    return Response(headers={**_headers(item), "Content-Length": str(item.size_bytes)})


@router.get("/{task_id}/recordings")
def recording_list(
    task_id: UUID,
    request: Request,
    response: Response,
    cursor: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    authorization: str | None = Header(default=None),
) -> RecordingPage:
    response.headers["Cache-Control"] = "no-store"
    return _service(request, authorization).list_recordings(task_id, cursor, limit)


@router.get("/{task_id}/recordings/{artifact_id}/download")
def recording_download(
    task_id: UUID,
    artifact_id: UUID,
    request: Request,
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    service = _service(request, authorization)
    item = service.metadata(task_id, artifact_id)
    return StreamingResponse(
        service.stream(task_id, artifact_id, item),
        headers={**_headers(item), "Content-Length": str(item.size_bytes)},
    )


@router.get("/{task_id}/recordings/{artifact_id}")
def recording_range(
    task_id: UUID,
    artifact_id: UUID,
    request: Request,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    authorization: str | None = Header(default=None),
) -> Response:
    service = _service(request, authorization)
    if (
        range_header is None
        or len(range_header) > 80
        or (match := _RANGE.fullmatch(range_header)) is None
    ):
        raise InvalidRequestError("recording_range_required", "Explicit bytes=start-end required")
    start, end = map(int, match.groups())
    item, body = service.read_range(task_id, artifact_id, start, end)
    return Response(
        content=body,
        status_code=206,
        headers={
            **_headers(item),
            "Content-Range": f"bytes {start}-{end}/{item.size_bytes}",
            "Content-Length": str(len(body)),
        },
    )
