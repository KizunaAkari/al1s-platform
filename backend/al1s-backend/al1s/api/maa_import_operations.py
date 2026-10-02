"""Opt-in asynchronous imports; legacy upload responses remain compatible."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, BackgroundTasks, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from al1s.api.maa_import_selection import selection_header
from al1s.app.service_state import service_state
from al1s.maa.archive import ParsedScriptArchive
from al1s.maa.archive_upload import MAX_UPLOAD_BYTES
from al1s.maa.errors import MaaDomainError
from al1s.maa.import_command_service import MaaArchiveImportCommandService
from al1s.maa.import_service import IMPORT_PROCESSING_LIMIT
from al1s.maa.types import ImportBatchRecord, ImportStatus

router = APIRouter()


def operation(batch: ImportBatchRecord) -> dict[str, object]:
    deadline = batch.created_at + IMPORT_PROCESSING_LIMIT
    state = batch.status.value
    if batch.error_code == "archive_import_cancelled":
        state = "cancelled"
    elif batch.status is ImportStatus.PROCESSING:
        state = "executing" if datetime.now(UTC) < deadline else "result_unknown"
    return {
        "operation_id": str(batch.batch_id), "state": state,
        "row_version": batch.row_version, "error_code": batch.error_code,
        "deadline": deadline.isoformat(),
        "can_cancel": batch.status is ImportStatus.PROCESSING,
    }


async def finish(
    service: MaaArchiveImportCommandService,
    parsed: ParsedScriptArchive,
    batch: ImportBatchRecord,
    slots: asyncio.Semaphore,
) -> None:
    try:
        await run_in_threadpool(service.finish_upload, parsed, batch, correlation_id=uuid4())
    except Exception as exc:
        # Import service persists failure; never log archive content.
        structlog.get_logger().warning("async_import_ended", error_type=type(exc).__name__)
    finally:
        slots.release()


@router.post("/imports/operations", status_code=202)
async def submit(
    request: Request, background: BackgroundTasks, response: Response
) -> dict[str, object]:
    selection = selection_header(request.headers.get("X-AL1S-Import-Selection"))
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/zip":
        raise MaaDomainError("archive_upload_type", "Content-Type must be application/zip", 415)
    slots = service_state(request).maa_archive_upload_slots
    if slots.locked():
        raise MaaDomainError("archive_upload_busy", "An import is still executing", 429)
    await slots.acquire()
    handed_off = False
    try:
        content = bytearray()
        try:
            async with asyncio.timeout(60):
                async for chunk in request.stream():
                    if len(content) + len(chunk) > MAX_UPLOAD_BYTES:
                        raise MaaDomainError("archive_upload_size", "Archive exceeds 32 MiB", 413)
                    content.extend(chunk)
        except TimeoutError as exc:
            raise MaaDomainError("archive_upload_timeout", "Archive upload timed out", 408) from exc
        service = service_state(request).maa_archive_import_commands
        parsed, batch, replayed = await run_in_threadpool(
            service.prepare_upload, bytes(content), **selection,
        )
        if not replayed:
            background.add_task(finish, service, parsed, batch, slots)
            handed_off = True
        response.headers["Cache-Control"] = "no-store"
        response.headers["Location"] = f"/api/v1/maa/imports/{batch.batch_id}/operation"
        return operation(batch)
    finally:
        if not handed_off:
            slots.release()


@router.get("/imports/{batch_id}/operation")
def status(batch_id: UUID, request: Request, response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    return operation(service_state(request).maa_catalog.get_import(batch_id))


class CancelBody(BaseModel):
    row_version: int = Field(ge=1)


@router.post("/imports/{batch_id}/cancel")
def cancel(
    batch_id: UUID, body: CancelBody, request: Request, response: Response
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    batch = service_state(request).maa_archive_import_commands.cancel(batch_id, body.row_version)
    return operation(batch)
