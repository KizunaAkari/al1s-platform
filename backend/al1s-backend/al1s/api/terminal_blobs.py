from __future__ import annotations

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response, status

from al1s.api.terminal_auth import authenticated_terminal_id
from al1s.app.service_state import service_state
from al1s.execution.blob_transfer import TerminalBlobMetadata, TerminalBlobTransferService
from al1s.execution.errors import InvalidRequestError

router = APIRouter()
_RANGE_PATTERN = re.compile(r"bytes=(0|[1-9][0-9]*)-(0|[1-9][0-9]*)\Z")


def _service(request: Request) -> TerminalBlobTransferService:
    return service_state(request).terminal_blob_transfer


def _headers(metadata: TerminalBlobMetadata) -> dict[str, str]:
    return {
        "Accept-Ranges": "bytes",
        "Content-Type": metadata.media_type,
        "ETag": f'"sha256:{metadata.sha256}"',
        "X-Content-SHA256": metadata.sha256,
    }


@router.head("/blobs/{blob_id}")
def head_blob(
    blob_id: UUID,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    metadata = _service(request).metadata(
        authenticated_terminal_id(request, authorization), blob_id
    )
    return Response(
        status_code=status.HTTP_200_OK,
        headers={**_headers(metadata), "Content-Length": str(metadata.size_bytes)},
    )


@router.get("/blobs/{blob_id}")
def download_blob_range(
    blob_id: UUID,
    request: Request,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    authorization: str | None = Header(default=None),
) -> Response:
    if range_header is None or (matched := _RANGE_PATTERN.fullmatch(range_header)) is None:
        raise InvalidRequestError(
            "blob_range_required",
            "A single explicit bytes=start-end Range header is required",
        )
    start, end_inclusive = (int(value) for value in matched.groups())
    metadata, body = _service(request).read_range(
        authenticated_terminal_id(request, authorization),
        blob_id,
        start=start,
        end_inclusive=end_inclusive,
    )
    return Response(
        content=body,
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        headers={
            **_headers(metadata),
            "Content-Length": str(len(body)),
            "Content-Range": f"bytes {start}-{end_inclusive}/{metadata.size_bytes}",
        },
    )
