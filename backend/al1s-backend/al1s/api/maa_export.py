import asyncio
from uuid import UUID

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from al1s.app.service_state import service_state
from al1s.maa.errors import MaaDomainError

router = APIRouter()


@router.get("/strategies/{strategy_id}/archive")
async def export_strategy(strategy_id: UUID, request: Request) -> Response:
    slots: asyncio.Semaphore = service_state(request).maa_export_slots
    if slots.locked():
        raise MaaDomainError("archive_busy", "An export is in progress; retry later", 429)
    async with slots:
        body = await run_in_threadpool(
            service_state(request).maa_exports.export_strategy, strategy_id
        )
    return Response(
        body,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="maa-strategy-{strategy_id}.zip"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/scripts/{script_id}/versions/{version_id}/archive")
async def export_script(script_id: UUID, version_id: UUID, request: Request) -> Response:
    slots: asyncio.Semaphore = service_state(request).maa_export_slots
    if slots.locked():
        raise MaaDomainError("archive_busy", "An export is in progress; retry later", 429)
    async with slots:
        body = await run_in_threadpool(
            service_state(request).maa_exports.export, script_id, version_id
        )
    return Response(
        body,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="maa-{script_id}-{version_id}.zip"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
