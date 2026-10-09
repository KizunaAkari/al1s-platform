import asyncio
import base64
import binascii
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Literal, TypeVar
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from al1s.meme_workshop.contracts import (
    CommandSource,
    ConnectionSettings,
    RenderedImage,
    RenderInputs,
    StrictModel,
    TemplateWrite,
)
from al1s.meme_workshop.images import MAX_BYTES
from al1s.meme_workshop.service import MemeWorkshopService

router = APIRouter()
T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)
MAX_REQUEST_BYTES = 56 * 1024 * 1024
MAX_ENCODED_BYTES = ((MAX_BYTES + 2) // 3) * 4
ImageData = Annotated[str, Field(max_length=MAX_ENCODED_BYTES)]
VersionHeader = Annotated[str, Header(alias="If-Match", max_length=20)]
OperationHeader = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


class ImageInputs(StrictModel):
    avatars: list[ImageData] = Field(default_factory=list, max_length=4)
    images: list[ImageData] = Field(default_factory=list, max_length=1)
    texts: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=4)

    def decoded(self) -> RenderInputs:
        try:
            avatars = [base64.b64decode(value, validate=True) for value in self.avatars]
            images = [base64.b64decode(value, validate=True) for value in self.images]
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid_image_encoding") from exc
        if any(not body or len(body) > MAX_BYTES for body in [*avatars, *images]):
            raise ValueError("invalid_image_size")
        if sum(map(len, self.texts)) > 200:
            raise ValueError("text_too_long")
        return RenderInputs(avatars, images, self.texts)


class PreviewRequest(ImageInputs):
    template: TemplateWrite


class ClaimRequest(StrictModel):
    source: CommandSource


class RenderRequest(ImageInputs):
    receipt_id: UUID
    keyword: str = Field(min_length=1, max_length=32)


class SendRequest(StrictModel):
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]+$")


class SettleRequest(SendRequest):
    status: Literal["sent", "failed", "unknown"]


def workshop(request: Request) -> MemeWorkshopService:
    from typing import cast

    return cast(MemeWorkshopService, request.app.state.meme_workshop)


def worker_id(request: Request) -> UUID:
    header = request.headers.get("authorization", "")
    credential = header[7:] if header.startswith("Bearer ") else ""
    from typing import cast

    from al1s.bots.service import BotService

    return cast(BotService, request.app.state.bot_service).worker_service_id(credential)


def expected_version(header: str) -> int:
    value = header.strip('"')
    if not value.isdigit():
        raise HTTPException(400, "invalid_row_version")
    return int(value)


def guarded[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except ValueError as exc:
        code = str(exc)
        status = (
            429
            if code == "render_busy"
            else 409
            if "conflict" in code
            else 404
            if code in {"template_not_found", "bot_not_found", "asset_not_found"}
            else 422
        )
        # Fixed domain codes and renderer validation only; no upstream replies.
        raise HTTPException(status, code) from exc


async def receive[M: BaseModel](request: Request, model: type[M]) -> M:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "meme_request_too_large")
        body.extend(chunk)
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(422, "invalid_meme_request") from exc


@asynccontextmanager
async def image_slot(request: Request) -> AsyncIterator[None]:
    slots = getattr(request.app.state, "meme_input_slots", None)
    if slots is None:
        slots = asyncio.Semaphore(2)
        request.app.state.meme_input_slots = slots
    if slots.locked():
        raise HTTPException(429, "render_busy")
    try:
        async with slots:
            async with asyncio.timeout(15):
                yield
    except TimeoutError as exc:
        raise HTTPException(504, "meme_deadline_exceeded") from exc


def image_response(result: RenderedImage) -> Response:
    return Response(
        result.body,
        media_type=result.media_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/bots/memes/templates")
def templates(
    request: Request, after_id: UUID | None = None, limit: int = Query(default=50, ge=1, le=50)
) -> dict[str, object]:
    rows = guarded(
        lambda: workshop(request).repository.list_templates(after_id=after_id, limit=limit + 1)
    )
    page = rows[:limit]
    return {"items": page, "next_cursor": str(page[-1].id) if len(rows) > limit else None}


@router.put("/bots/memes/templates/{identity}")
def save_template(
    identity: UUID,
    body: TemplateWrite,
    request: Request,
    if_match: VersionHeader,
    idempotency_key: OperationHeader,
) -> object:
    return guarded(
        lambda: workshop(request).repository.save(
            identity,
            body,
            expected_version=expected_version(if_match),
            operation_id=idempotency_key,
        )
    )


@router.delete("/bots/memes/templates/{identity}", status_code=204)
def delete_template(
    identity: UUID, request: Request, if_match: VersionHeader, idempotency_key: OperationHeader
) -> Response:
    guarded(
        lambda: workshop(request).repository.delete(
            identity,
            expected_version=expected_version(if_match),
            operation_id=idempotency_key,
        )
    )
    return Response(status_code=204)


@router.get("/bots/memes/settings/{bot_id}")
def settings(bot_id: UUID, request: Request) -> object:
    return guarded(lambda: workshop(request).repository.get_settings(bot_id))


@router.put("/bots/memes/settings/{bot_id}")
def save_settings(
    bot_id: UUID,
    body: ConnectionSettings,
    request: Request,
    if_match: VersionHeader,
    idempotency_key: OperationHeader,
) -> object:
    return guarded(
        lambda: workshop(request).repository.save_settings(
            bot_id,
            body,
            expected_version=expected_version(if_match),
            operation_id=idempotency_key,
        )
    )


@router.post("/bots/memes/assets")
async def upload_asset(request: Request) -> object:
    async with image_slot(request):
        if request.headers.get("content-type", "").split(";", 1)[0] not in {
            "image/png",
            "image/jpeg",
            "image/webp",
        }:
            raise HTTPException(415, "unsupported_asset_type")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BYTES:
                raise HTTPException(413, "asset_too_large")
            body.extend(chunk)
        return await run_in_threadpool(
            lambda: guarded(lambda: workshop(request).assets.upload(bytes(body)))
        )


@router.get("/bots/memes/assets/{identity}")
async def read_asset(identity: UUID, request: Request) -> Response:
    async with image_slot(request):
        body = await run_in_threadpool(
            lambda: guarded(lambda: workshop(request).assets.read(identity))
        )
        return Response(body, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/bots/memes/preview")
async def preview(request: Request) -> Response:
    async with image_slot(request):
        body = await receive(request, PreviewRequest)
        result = await run_in_threadpool(
            lambda: guarded(
                lambda: workshop(request).preview(
                    body.template,
                    body.decoded(),
                )
            )
        )
        return image_response(result)


@router.get("/bots/worker/memes/catalog")
def worker_catalog(request: Request) -> object:
    identity = worker_id(request)
    return guarded(lambda: workshop(request).catalog(identity))


@router.post("/bots/worker/memes/claim")
def worker_claim(body: ClaimRequest, request: Request) -> dict[str, str | None]:
    identity = worker_id(request)
    receipt = guarded(lambda: workshop(request).repository.claim(identity, body.source))
    return {"receipt_id": str(receipt) if receipt else None}


@router.post("/bots/worker/memes/render")
async def worker_render(request: Request) -> Response:
    async with image_slot(request):
        identity = worker_id(request)
        body = await receive(request, RenderRequest)
        result = await run_in_threadpool(
            lambda: guarded(
                lambda: workshop(request).render_command(
                    identity,
                    body.receipt_id,
                    body.keyword,
                    body.decoded(),
                )
            )
        )
        return image_response(result)


@router.post("/bots/worker/memes/{receipt}/send")
def worker_send(receipt: UUID, body: SendRequest, request: Request) -> dict[str, bool]:
    identity = worker_id(request)
    return {
        "allowed": guarded(
            lambda: workshop(request).repository.start_send(
                identity,
                receipt,
                error_code=body.error_code,
            )
        )
    }


@router.post("/bots/worker/memes/{receipt}/result", status_code=204)
def worker_result(receipt: UUID, body: SettleRequest, request: Request) -> Response:
    identity = worker_id(request)
    guarded(
        lambda: workshop(request).repository.settle(identity, receipt, body.status, body.error_code)
    )
    return Response(status_code=204)
