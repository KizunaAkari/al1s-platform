import asyncio
from dataclasses import asdict
from uuid import uuid4

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from al1s.api.maa_catalog_schemas import ImportBatchResponse
from al1s.api.maa_import_selection import selection_header
from al1s.app.service_state import service_state
from al1s.maa.archive_upload import MAX_UPLOAD_BYTES
from al1s.maa.errors import MaaDomainError

router = APIRouter()


@router.post("/imports/upload", response_model=ImportBatchResponse)
async def upload_archive(request: Request, response: Response) -> ImportBatchResponse:
    selection = selection_header(request.headers.get("X-AL1S-Import-Selection"))
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/zip":
        raise MaaDomainError("archive_upload_type", "Content-Type must be application/zip", 415)
    slots = service_state(request).maa_archive_upload_slots
    if slots.locked():
        raise MaaDomainError(
            "archive_upload_busy", "An archive is being imported; retry later", 429
        )
    async with slots:
        body = bytearray()
        try:
            async with asyncio.timeout(60):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_UPLOAD_BYTES:
                        raise MaaDomainError("archive_upload_size", "Archive exceeds 32 MiB", 413)
                    body.extend(chunk)
        except TimeoutError as exc:
            raise MaaDomainError("archive_upload_timeout", "Archive upload timed out", 408) from exc
        result = await run_in_threadpool(
            service_state(request).maa_archive_import_commands.import_uploaded_archive,
            bytes(body),
            correlation_id=uuid4(),
            **selection,
        )
    response.status_code = 200 if result.replayed else 201
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return ImportBatchResponse(**asdict(result.batch))
