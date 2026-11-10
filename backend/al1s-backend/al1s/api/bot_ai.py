from typing import Annotated, Literal
from pathlib import Path
from io import BytesIO
from uuid import UUID, uuid4
import asyncio
import os
import re
import time

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool

from al1s.bot_ai.settings import AiChatProfileWrite, AiSettingsWrite
from al1s.bot_ai.managed.smart_contract import StickerCommand
from al1s.bot_ai.sticker_import import MAX_ARCHIVE, extract_images

router = APIRouter()


class ProviderTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["chat", "vision", "stt", "tts"]


class ClearSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_kind: Literal["qq_group", "qq_private"]
    target_id: str


def _error(error: ValueError) -> HTTPException:
    code = str(error)
    if code == "ai_comfyui_local_address_required":
        return HTTPException(422, "本地视觉仅支持本机的 8188 端口，请使用 http://host.docker.internal:8188。")
    if code == "ai_sticker_scope_invalid":
        return HTTPException(403, code)
    if code == "ai_sticker_vision_unavailable":
        return HTTPException(409, "请先启用并配置该群可用的图片理解模型。")
    if code in {"ai_sticker_unavailable", "ai_sticker_unconfirmed", "ai_control_busy"}:
        return HTTPException(503, code)
    if code == "ai_proxy_unavailable":
        return HTTPException(422, "未检测到可用的容器 HTTPS 代理，配置未保存。")
    if code == "ai_proxy_credentials_unsupported":
        return HTTPException(422, "当前容器代理包含认证信息，暂不支持用于模型；配置未保存。")
    if code in {"qq_bot_not_found", "ai_profile_not_found"}:
        return HTTPException(404, code)
    if code in {"ai_settings_conflict", "idempotency_conflict", "ai_sync_pending",
                "ai_session_busy", "ai_clear_unconfirmed", "ai_clear_unavailable",
                "ai_profile_name_conflict", "ai_profile_default_required", "ai_profile_unconfigured"}:
        return HTTPException(409, code)
    return HTTPException(422, "ai_settings_invalid")


def _sticker_preview(value, root):
    from PIL import Image
    if not isinstance(value, str):
        raise ValueError("ai_sticker_unavailable")
    path = Path(value).resolve()
    root = Path(root).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("ai_sticker_unavailable")
    body = path.read_bytes()
    with Image.open(BytesIO(body), formats=["PNG", "JPEG", "GIF", "WEBP"]) as image:
        mime = Image.MIME[image.format]
    return body, mime


@router.post("/bots/ai/stickers/{service_id}")
async def stickers(service_id: UUID, data: StickerCommand, request: Request):
    controller = request.app.state.ai_controller
    if controller is None:
        raise HTTPException(503, "ai_engine_unavailable")
    try:
        result = await controller.stickers(service_id, data)
        if data.action == "preview":
            body, mime = await run_in_threadpool(
                _sticker_preview, result.get("media_ref"), controller.engine.stickers.categories_root)
            return Response(body, media_type=mime,
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return result
    except ValueError as error:
        raise _error(error) from error
    except OSError as error:
        raise HTTPException(503, "ai_sticker_unavailable") from error


@router.post("/bots/ai/stickers/{service_id}/import/{group_id}/{offset}")
async def import_stickers(service_id: UUID, group_id: str, offset: int, request: Request):
    if request.query_params or not re.fullmatch(r"[1-9][0-9]{0,19}", group_id) or not 0 <= offset <= 100:
        raise HTTPException(400, "ai_sticker_import_invalid")
    controller = request.app.state.ai_controller
    if controller is None:
        raise HTTPException(503, "ai_engine_unavailable")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_ARCHIVE:
            raise HTTPException(413, "文件超过 32 MiB。")
        body.extend(chunk)
    try:
        images = await run_in_threadpool(extract_images, bytes(body))
        if offset > len(images):
            raise ValueError("ai_sticker_import_invalid")
        media, index, queued = controller.sessions.media, offset, 0
        started, status = time.monotonic(), "complete"
        while index < len(images):
            if time.monotonic() - started >= 7:
                status = "continue"
                break
            extension, content = images[index]
            identity = uuid4()
            directory = media.directory(identity)
            directory.mkdir(mode=0o2770)
            os.chmod(directory, 0o2770)
            target = directory / ("import" + extension)
            try:
                descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
                result = await controller.import_sticker(service_id, group_id, identity, str(target))
            finally:
                await asyncio.to_thread(media.cleanup, identity)
            state = result.get("status")
            if state in {"busy", "full"}:
                status = state
                break
            if state != "queued":
                raise ValueError("ai_sticker_unavailable")
            queued += 1
            index += 1
        return {"status": status, "queued": queued, "total": len(images),
                "next_offset": index if index < len(images) else None}
    except ValueError as error:
        if str(error).startswith("ai_sticker_archive") or str(error) == "ai_sticker_image_invalid":
            raise HTTPException(422, "图片或 ZIP 无效，或超过数量、大小限制。") from error
        if str(error) == "ai_sync_pending":
            raise HTTPException(409, "请等待 AI 配置同步后再导入。") from error
        if str(error) == "ai_sticker_scope_invalid":
            raise HTTPException(403, "目标群未授权，请刷新配置后重试。") from error
        raise _error(error) from error
    except OSError as error:
        raise HTTPException(503, "ai_sticker_unavailable") from error


@router.get("/bots/ai/settings/{service_id}")
def read_settings(service_id: UUID, request: Request) -> dict:
    try:
        return request.app.state.ai_repository.get_settings(service_id).model_dump(mode="json")
    except ValueError as error:
        raise _error(error) from error


@router.put("/bots/ai/settings/{service_id}")
async def save_settings(
    service_id: UUID,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> dict:
    version = if_match.strip('"')
    if not version.isdigit():
        raise HTTPException(400, "invalid_row_version")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 65536:
            raise HTTPException(413, "ai_settings_too_large")
        body.extend(chunk)
    try:
        data = AiSettingsWrite.model_validate_json(bytes(body))
    except ValidationError as error:
        raise HTTPException(422, "ai_settings_invalid") from error
    try:
        result = await run_in_threadpool(request.app.state.ai_repository.save_settings,
            service_id, data, expected_version=int(version), operation_id=operation)
    except ValueError as error:
        raise _error(error) from error
    controller = request.app.state.ai_controller
    if controller is not None:
        controller.request_apply(service_id, result.desired_version)
    return result.model_dump(mode="json")


@router.get("/bots/ai/status/{service_id}")
def status(service_id: UUID, request: Request) -> dict:
    try:
        record = request.app.state.ai_repository.get_settings(service_id)
    except ValueError as error:
        raise _error(error) from error
    controller = request.app.state.ai_controller
    state = controller.status(service_id) if controller is not None else {"state": "unavailable"}
    return {"desired_version": record.desired_version, "applied_version": record.applied_version,
            "synchronized": (record.desired_version > 0
                             and record.desired_version == record.applied_version),
            "last_error": record.last_error, **state}


@router.get("/bots/ai/chat-profiles/{service_id}")
def chat_profiles(service_id: UUID, request: Request) -> dict:
    try:
        profiles = request.app.state.ai_repository.list_chat_profiles(service_id)
        return {"items": [item.model_dump(mode="json") for item in profiles]}
    except ValueError as error:
        raise _error(error) from error


@router.put("/bots/ai/chat-profiles/{service_id}/{profile_id}")
async def save_chat_profile(
    service_id: UUID, profile_id: UUID, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> dict:
    version = if_match.strip('"')
    if not version.isdigit():
        raise HTTPException(400, "invalid_row_version")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 65536:
            raise HTTPException(413, "ai_settings_too_large")
        body.extend(chunk)
    try:
        data = AiChatProfileWrite.model_validate_json(bytes(body))
    except ValidationError as error:
        raise HTTPException(422, "ai_settings_invalid") from error
    try:
        repository = request.app.state.ai_repository
        result = await run_in_threadpool(repository.save_chat_profile, service_id, profile_id, data,
                                         expected_version=int(version), operation_id=operation)
        settings = await run_in_threadpool(repository.get_settings, service_id)
    except ValueError as error:
        raise _error(error) from error
    if request.app.state.ai_controller is not None:
        request.app.state.ai_controller.request_apply(service_id, settings.desired_version)
    return {"profile": result.model_dump(mode="json"), "desired_version": settings.desired_version}


@router.delete("/bots/ai/chat-profiles/{service_id}/{profile_id}")
async def delete_chat_profile(
    service_id: UUID, profile_id: UUID, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> dict:
    version = if_match.strip('"')
    if not version.isdigit():
        raise HTTPException(400, "invalid_row_version")
    try:
        result = await run_in_threadpool(request.app.state.ai_repository.delete_chat_profile,
            service_id, profile_id, expected_version=int(version), operation_id=operation)
    except ValueError as error:
        raise _error(error) from error
    if request.app.state.ai_controller is not None:
        request.app.state.ai_controller.request_apply(service_id, result["desired_version"])
    return result


@router.post("/bots/ai/chat-profiles/{service_id}/{profile_id}/test")
async def test_chat_profile(service_id: UUID, profile_id: UUID, request: Request) -> dict:
    controller = request.app.state.ai_controller
    if controller is None:
        raise HTTPException(503, "ai_engine_unavailable")
    try:
        return await controller.test_chat_profile(service_id, profile_id)
    except ValueError as error:
        raise _error(error) from error


@router.get("/bots/ai/requests/{service_id}")
def requests(service_id: UUID, request: Request, after_id: UUID | None = None,
             limit: int = 20) -> dict:
    if not 1 <= limit <= 50:
        raise HTTPException(422, "ai_request_limit_invalid")
    try:
        return request.app.state.ai_repository.list_requests(service_id, after_id=after_id,
                                                              limit=limit)
    except ValueError as error:
        raise _error(error) from error


@router.post("/bots/ai/providers/test/{service_id}")
async def test_provider(service_id: UUID, data: ProviderTest, request: Request) -> dict:
    controller = request.app.state.ai_controller
    if controller is None:
        raise HTTPException(503, "ai_engine_unavailable")
    try:
        return await controller.test_provider(service_id, data.role)
    except ValueError as error:
        raise _error(error) from error


@router.post("/bots/ai/sessions/{service_id}/clear")
async def clear_session(service_id: UUID, data: ClearSession, request: Request) -> dict:
    if not data.target_id.isascii() or not data.target_id.isdigit() or len(data.target_id) > 20:
        raise HTTPException(422, "ai_target_invalid")
    controller = request.app.state.ai_controller
    if controller is None:
        raise HTTPException(503, "ai_engine_unavailable")
    try:
        return await controller.clear_session(service_id, data.scope_kind, data.target_id)
    except ValueError as error:
        raise _error(error) from error
