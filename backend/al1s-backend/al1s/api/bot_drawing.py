import json
import re
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from al1s.bot_drawing.contracts import InputBindings, PresetDraft
from al1s.bot_ai.settings import ADAPTERS, AiProviderWrite

router = APIRouter(prefix="/bots/ai/drawing/{service_id}")
MAX_BODY = 320 * 1024


class SettingsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    keep_drawing_warm: StrictBool
    default_text_preset_id: UUID | None = None
    default_edit_preset_id: UUID | None = None
    default_online_preset_id: UUID | None = None
    allow_private: StrictBool | None = None
    event_ws_url: str | None = Field(default=None, max_length=1024)
    comfy_base_url: str | None = Field(default=None, max_length=1024)
    groups: list[str] | None = Field(default=None, max_length=100)


class PromptProviderWrite(AiProviderWrite):
    name: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_drawing_provider(self):
        if self.adapter_kind not in ADAPTERS["chat"]:
            raise ValueError("drawing_prompt_adapter_invalid")
        return self


class PromptSelectionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: UUID | None = None


class PresetWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=32)
    kind: Literal["txt2img", "image_edit"]
    graph: dict
    bindings: InputBindings
    aliases: dict[str, str] = Field(default_factory=dict, max_length=100)
    enabled: bool = False


class OnlinePresetWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=32)
    model_id: Literal["gpt-image-2.6-flare", "gpt-image-2.6-sunburst"]
    base_url: str = Field(default="", max_length=1024)
    network_mode: Literal["direct", "proxy"] = "direct"
    size: Literal["auto", "1024x1024", "1024x1536", "1536x1024"] = "auto"
    quality: Literal["auto", "low", "medium", "high", "xhigh", "max"] = "auto"
    use_prompt_model: StrictBool = False
    enabled: StrictBool = False
    secret_input: SecretStr | None = Field(default=None, repr=False)
    clear_secret: StrictBool = False

    @model_validator(mode="after")
    def validate_provider(self):
        AiProviderWrite(adapter_kind="openai_responses", model_id=self.model_id, base_url=self.base_url,
                        network_mode=self.network_mode, secret_input=self.secret_input, clear_secret=self.clear_secret)
        return self


class GroupWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["green", "r18"]
    restrict_workflows: StrictBool | None = None
    allowed_preset_ids: list[UUID] | None = Field(default=None, max_length=20)


class EmptyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BatchDeleteWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_ids: list[UUID] = Field(min_length=1, max_length=50)


def _version(value: str) -> int:
    if not re.fullmatch(r'(?:"[0-9]{1,10}"|[0-9]{1,10})', value):
        raise HTTPException(400, "drawing_version_invalid")
    value = value.strip('"')
    if int(value) > 2 ** 31 - 1:
        raise HTTPException(400, "drawing_version_invalid")
    return int(value)


async def _read(request: Request, model):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise HTTPException(413, "工作流请求超过大小限制。")
        body.extend(chunk)
    try:
        data = json.loads(body)
        return model.model_validate(data)
    except (ValueError, TypeError, ValidationError):
        raise HTTPException(422, "绘图配置格式无效。") from None


async def _preview_form(request: Request) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 9 * 1024 * 1024:
            raise HTTPException(413, "试运行请求超过大小限制。")
        body.extend(chunk)
    async def stream():
        yield bytes(body)
    try:
        form = await MultiPartParser(request.headers, stream(), max_files=1, max_fields=3,
                                     max_part_size=8 * 1024 * 1024).parse()
    except (MultiPartException, ValueError, AssertionError):
        raise HTTPException(422, "试运行参数无效。") from None
    try:
        if (set(form.keys()) - {"positive", "negative", "mode", "image"}
                or any(len(form.getlist(name)) != 1 for name in form.keys())):
            raise HTTPException(422, "试运行参数无效。")
        positive, negative, mode = form.get("positive"), form.get("negative", ""), form.get("mode", "green")
        upload = form.get("image")
        if (not isinstance(positive, str) or not isinstance(negative, str) or not positive.strip()
                or len(positive) + len(negative) > 2000 or mode not in {"green", "r18"}
                or upload is not None and not isinstance(upload, UploadFile)):
            raise HTTPException(422, "试运行参数无效。")
        image = await upload.read(8 * 1024 * 1024 + 1) if upload is not None else None
        if image is not None and (not image or len(image) > 8 * 1024 * 1024):
            raise HTTPException(413, "参考图超过 8 MiB 或为空。")
        return {"positive": positive, "negative": negative, "mode": mode, "image": image}
    finally:
        await form.close()


def _error(error: Exception) -> HTTPException:
    if isinstance(error, (httpx.HTTPError, OSError, TimeoutError)):
        return HTTPException(503, "ComfyUI 资源服务暂不可用，请检查连接。")
    code = str(error)
    if code in {"qq_bot_not_found", "drawing_preset_not_found", "drawing_task_not_found", "drawing_prompt_not_found"}:
        return HTTPException(404, code)
    if code == "drawing_media_expired":
        return HTTPException(410, code)
    if code == "drawing_legacy_warm_unsupported":
        return HTTPException(409, "无限常驻已停用，请刷新页面使用空闲 60 秒自动释放。")
    if code in {"drawing_settings_conflict", "drawing_group_mode_conflict", "drawing_preset_conflict",
                "drawing_dependency_changed", "drawing_send_retry_invalid", "drawing_prompt_conflict",
                "drawing_prompt_name_duplicate", "drawing_task_busy", "idempotency_conflict"}:
        return HTTPException(409, code)
    if code in {"drawing_resources_unavailable", "comfy_resource_unsupported"}:
        return HTTPException(503, "共享 ComfyUI 资源服务尚未就绪。")
    if code == "drawing_scope_denied":
        return HTTPException(403, code)
    return HTTPException(422, code if re.fullmatch(r"[a-z0-9_]{1,80}", code) else "drawing_config_invalid")


@router.get("/settings")
async def settings(service_id: UUID, request: Request):
    try:
        return await run_in_threadpool(request.app.state.drawing_repository.get_settings, service_id)
    except ValueError as error:
        raise _error(error) from error


@router.put("/settings")
async def save_settings(service_id: UUID, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    data = await _read(request, SettingsWrite)
    try:
        values = data.model_dump()
        if "default_online_preset_id" not in data.model_fields_set:
            values.pop("default_online_preset_id")
        result = await run_in_threadpool(request.app.state.drawing_repository.save_settings, service_id,
            **values, expected_version=_version(if_match), operation_id=operation)
        apply = getattr(request.app.state.drawing_configuration, "request_apply", None)
        if apply is not None:
            apply(service_id)
        return result.json_dict()
    except ValueError as error:
        raise _error(error) from error


@router.get("/group-modes")
async def groups(service_id: UUID, request: Request):
    try:
        return {"items": await run_in_threadpool(request.app.state.drawing_repository.list_group_modes, service_id)}
    except ValueError as error:
        raise _error(error) from error


@router.get("/prompt-models")
async def prompt_models(service_id: UUID, request: Request):
    try:
        return await run_in_threadpool(request.app.state.drawing_repository.list_prompt_models, service_id)
    except ValueError as error:
        raise _error(error) from error


@router.post("/prompt-models", status_code=201)
async def create_prompt_model(service_id: UUID, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    data = await _read(request, PromptProviderWrite)
    try:
        return await run_in_threadpool(request.app.state.drawing_repository.save_prompt_model,
            service_id, data, profile_id=None, expected_version=_version(if_match), operation_id=operation)
    except ValueError as error:
        raise _error(error) from error


@router.put("/prompt-models/active")
async def activate_prompt_model(service_id: UUID, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    data = await _read(request, PromptSelectionWrite)
    try:
        return await run_in_threadpool(request.app.state.drawing_repository.activate_prompt_model,
            service_id, data.profile_id, expected_version=_version(if_match), operation_id=operation)
    except ValueError as error:
        raise _error(error) from error


@router.put("/prompt-models/{profile_id}")
async def update_prompt_model(service_id: UUID, profile_id: UUID, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    data = await _read(request, PromptProviderWrite)
    try:
        return await run_in_threadpool(request.app.state.drawing_repository.save_prompt_model,
            service_id, data, profile_id=profile_id, expected_version=_version(if_match), operation_id=operation)
    except ValueError as error:
        raise _error(error) from error


@router.delete("/prompt-models/{profile_id}")
async def delete_prompt_model(service_id: UUID, profile_id: UUID, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    try:
        return await run_in_threadpool(request.app.state.drawing_repository.delete_prompt_model,
            service_id, profile_id, expected_version=_version(if_match), operation_id=operation)
    except ValueError as error:
        raise _error(error) from error


@router.post("/prompt-models/{profile_id}/test")
async def test_prompt_model(service_id: UUID, profile_id: UUID, request: Request):
    await _read(request, EmptyWrite)
    service = getattr(request.app.state, "drawing_service", None)
    if service is None:
        raise HTTPException(503, "绘图服务尚未就绪。")
    try:
        return await service.manager.test_model(service_id, profile_id)
    except (ValueError, httpx.HTTPError, OSError, TimeoutError) as error:
        raise _error(error) from error


@router.put("/group-modes/{group_id}")
async def save_group(service_id: UUID, group_id: str, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    if not re.fullmatch(r"[1-9][0-9]{0,19}", group_id):
        raise HTTPException(422, "drawing_group_invalid")
    data = await _read(request, GroupWrite)
    try:
        scope = {} if data.restrict_workflows is None and data.allowed_preset_ids is None else {
            "restrict_workflows": data.restrict_workflows, "allowed_preset_ids": data.allowed_preset_ids}
        version = await run_in_threadpool(request.app.state.drawing_repository.save_group_mode,
            service_id, group_id, data.mode, expected_version=_version(if_match), operation_id=operation, **scope)
        return {"group_id": group_id, "mode": data.mode, "row_version": version,
                **({"restrict_workflows": data.restrict_workflows,
                    "allowed_preset_ids": [str(item) for item in data.allowed_preset_ids]} if scope else {})}
    except ValueError as error:
        raise _error(error) from error


@router.get("/presets")
async def presets(service_id: UUID, request: Request):
    try:
        return {"items": await run_in_threadpool(request.app.state.drawing_repository.list_presets, service_id)}
    except ValueError as error:
        raise _error(error) from error


@router.get("/presets/{preset_id}")
async def preset(service_id: UUID, preset_id: UUID, request: Request):
    try:
        return await run_in_threadpool(request.app.state.drawing_repository.get_preset, service_id, preset_id)
    except ValueError as error:
        raise _error(error) from error


async def _save_preset(service_id, preset_id, request, if_match, operation):
    data = await _read(request, PresetWrite)
    try:
        draft = PresetDraft(name=data.name, kind=data.kind, graph=data.graph, bindings=data.bindings)
        return await request.app.state.drawing_configuration.save_preset(service_id, draft, data.aliases,
            enabled=data.enabled, preset_id=preset_id, expected_version=_version(if_match), operation_id=operation)
    except (ValueError, httpx.HTTPError, OSError, TimeoutError) as error:
        raise _error(error) from error


@router.post("/presets")
async def create_preset(service_id: UUID, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    return await _save_preset(service_id, None, request, if_match, operation)


@router.post("/online-presets")
async def create_online_preset(service_id: UUID, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    data = await _read(request, OnlinePresetWrite)
    try:
        return await run_in_threadpool(request.app.state.drawing_repository.save_online_preset,
            service_id, data, preset_id=None, expected_version=_version(if_match), operation_id=operation)
    except ValueError as error:
        raise _error(error) from error


@router.put("/online-presets/{preset_id}")
async def update_online_preset(service_id: UUID, preset_id: UUID, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    data = await _read(request, OnlinePresetWrite)
    try:
        return await run_in_threadpool(request.app.state.drawing_repository.save_online_preset,
            service_id, data, preset_id=preset_id, expected_version=_version(if_match), operation_id=operation)
    except ValueError as error:
        raise _error(error) from error


@router.put("/presets/{preset_id}")
async def save_preset(service_id: UUID, preset_id: UUID, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    return await _save_preset(service_id, preset_id, request, if_match, operation)


@router.post("/presets/{preset_id}/check")
async def check_preset(service_id: UUID, preset_id: UUID, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    await _read(request, EmptyWrite)
    try:
        return await request.app.state.drawing_configuration.check_preset(service_id, preset_id,
                                                                          _version(if_match), operation)
    except (ValueError, httpx.HTTPError, OSError, TimeoutError) as error:
        raise _error(error) from error


@router.post("/presets/{preset_id}/preview", status_code=202)
async def preview_preset(service_id: UUID, preset_id: UUID, request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    data = await _preview_form(request)
    if getattr(request.app.state, "drawing_service", None) is None:
        raise HTTPException(503, "绘图服务尚未就绪。")
    try:
        task_id = await request.app.state.drawing_service.submit_preview(service_id, preset_id,
            **data, expected_version=_version(if_match), operation_id=operation)
        return {"task_id": str(task_id)}
    except (ValueError, OSError, TimeoutError) as error:
        raise _error(error) from error


@router.get("/resources")
async def resources(service_id: UUID, request: Request):
    try:
        return await request.app.state.drawing_configuration.resources(service_id)
    except (ValueError, httpx.HTTPError, OSError, TimeoutError) as error:
        raise _error(error) from error


@router.post("/resources/release")
async def release_resources(service_id: UUID, request: Request):
    await _read(request, EmptyWrite)
    try:
        status = await request.app.state.drawing_configuration.release_idle(service_id)
        return {"status": status}
    except (ValueError, httpx.HTTPError, OSError, TimeoutError) as error:
        raise _error(error) from error


@router.post("/resources/warmup")
async def warmup_vision(service_id: UUID, request: Request):
    await _read(request, EmptyWrite)
    controller = getattr(request.app.state, "ai_controller", None)
    if controller is None:
        raise HTTPException(503, "图片理解尚未就绪。")
    try:
        settings = await run_in_threadpool(request.app.state.ai_repository.get_settings, service_id)
        provider = settings.providers.get('vision')
        if (not settings.enabled or not settings.vision_enabled or provider is None
                or provider.adapter_kind != 'al1s_comfyui_vision'
                or not await request.app.state.drawing_configuration.vision_ready(service_id)):
            raise ValueError('drawing_resources_unavailable')
        return await controller.test_provider(service_id, 'vision')
    except (ValueError, httpx.HTTPError, OSError, TimeoutError) as error:
        raise _error(error) from error


@router.get("/tasks")
async def tasks(service_id: UUID, request: Request, after_id: UUID | None = None,
                limit: Annotated[int, Query(ge=1, le=50)] = 20,
                view: Literal["all", "active", "history"] = "all"):
    try:
        result = await run_in_threadpool(request.app.state.drawing_repository.list_tasks, service_id,
                                         after_id=after_id, limit=limit, view=view)
        runner = getattr(request.app.state, "drawing_runner", None)
        online_positions = (await run_in_threadpool(request.app.state.drawing_repository.online_positions)
                            if any(item.get("online_request_state") is not None for item in result["items"]) else {})
        for item in result["items"]:
            queue = getattr(runner, "gpu_queue", None)
            item["queue_position"] = (online_positions.get(item["id"]) if item.get("online_request_state") is not None
                                      else queue.position(item["id"]) if queue is not None else None)
            item["progress"] = runner.progress(item["id"]) if runner is not None else None
        return result
    except ValueError as error:
        raise _error(error) from error


@router.post("/tasks/batch-delete")
async def delete_tasks(service_id: UUID, request: Request,
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    data = await _read(request, BatchDeleteWrite)
    runner = getattr(request.app.state, "drawing_runner", None)
    if runner is None:
        raise HTTPException(503, "绘图服务尚未就绪。")
    try:
        task_ids = await run_in_threadpool(request.app.state.drawing_repository.begin_delete_tasks,
                                           service_id, data.task_ids, operation_id=operation)
        return await runner.delete_history(task_ids)
    except ValueError as error:
        raise _error(error) from error


@router.get("/tasks/{task_id}")
async def task(service_id: UUID, task_id: UUID, request: Request):
    try:
        result = await run_in_threadpool(request.app.state.drawing_repository.get_task, service_id, task_id)
        runner = getattr(request.app.state, "drawing_runner", None)
        queue = getattr(runner, "gpu_queue", None)
        result["queue_position"] = ((await run_in_threadpool(request.app.state.drawing_repository.online_positions)).get(str(task_id))
                                    if result.get("online_request_state") is not None
                                    else queue.position(str(task_id)) if queue is not None else None)
        result["progress"] = runner.progress(str(task_id)) if runner is not None else None
        return result
    except ValueError as error:
        raise _error(error) from error


@router.get("/tasks/{task_id}/image")
async def task_image(service_id: UUID, task_id: UUID, request: Request):
    try:
        reference, checksum = await run_in_threadpool(request.app.state.drawing_repository.preview_media, service_id, task_id)
        body = await run_in_threadpool(request.app.state.drawing_media.read, reference, expected_sha256=checksum)
        media_type = "image/gif" if reference.endswith("/output.gif") else "image/png"
        return Response(body, media_type=media_type, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except FileNotFoundError:
        raise HTTPException(410, "drawing_media_expired") from None
    except (ValueError, OSError) as error:
        raise _error(error) from error


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(service_id: UUID, task_id: UUID, request: Request,
                      operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    await _read(request, EmptyWrite)
    try:
        status = await run_in_threadpool(request.app.state.drawing_repository.request_cancel,
                                         service_id, task_id, operation_id=operation)
        runner = getattr(request.app.state, "drawing_runner", None)
        if runner is not None:
            cancelled = getattr(runner, "cancelled", None)
            if cancelled is not None:
                await cancelled(task_id)
            else:
                runner.wake()
        return {"status": status}
    except ValueError as error:
        raise _error(error) from error


@router.post("/tasks/{task_id}/retry-send")
async def retry_send(service_id: UUID, task_id: UUID, request: Request,
                     operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    await _read(request, EmptyWrite)
    try:
        queued = await run_in_threadpool(request.app.state.drawing_repository.retry_send,
                                         service_id, task_id, operation_id=operation)
        runner = getattr(request.app.state, "drawing_runner", None)
        if runner is not None:
            runner.wake()
        return {"queued": queued}
    except ValueError as error:
        raise _error(error) from error
