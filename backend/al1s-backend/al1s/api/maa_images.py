import asyncio
from uuid import UUID

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from al1s.app.service_state import service_state
from al1s.maa.errors import MaaDomainError
from al1s.maa.image_resources import MAX_IMAGE_BYTES, MaaImageResourceService

router = APIRouter()


@router.get("/scripts/{script_id}/versions/{version_id}/images/{blob_id}")
async def read_image(
    script_id: UUID, version_id: UUID, blob_id: UUID, request: Request
) -> Response:
    slots: asyncio.Semaphore = service_state(request).maa_image_slots
    if slots.locked():
        raise MaaDomainError("image_busy", "Image operations are busy; retry later", 429)
    async with slots:
        service: MaaImageResourceService = service_state(request).maa_images
        body = await run_in_threadpool(service.read, script_id, version_id, blob_id)
    return Response(
        body,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/scripts/{script_id}/images")
async def upload_image(script_id: UUID, request: Request) -> dict[str, object]:
    if request.headers.get("content-type", "").split(";", 1)[0] != "image/png":
        raise MaaDomainError("unsupported_image_type", "Content-Type must be image/png", 415)
    slots: asyncio.Semaphore = service_state(request).maa_image_slots
    if slots.locked():
        raise MaaDomainError("image_upload_busy", "Image uploads are busy; retry later", 429)
    async with slots:
        return await _receive_image(script_id, request)


async def _receive_image(script_id: UUID, request: Request) -> dict[str, object]:
    body = bytearray()
    try:
        async with asyncio.timeout(30):
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_IMAGE_BYTES:
                    raise MaaDomainError("image_too_large", "PNG exceeds 16 MiB", 413)
                body.extend(chunk)
    except TimeoutError as exc:
        raise MaaDomainError("image_upload_timeout", "Image upload timed out", 408) from exc
    service: MaaImageResourceService = service_state(request).maa_images
    return await run_in_threadpool(service.upload, script_id, bytes(body))
