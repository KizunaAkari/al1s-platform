import asyncio
import hashlib
import logging
from uuid import UUID

import httpx

from al1s.bot_ai.managed.smart_contract import local_endpoint
from .capabilities import _POLICIES, _V8, build_capabilities, compatible_inspection
from .comfy import DrawingComfyClient
from .contracts import PresetDraft
from .workflow import validate_workflow


def _classes(draft: PresetDraft) -> list[str]:
    result = set()
    for node_id, node in draft.graph.items():
        if not isinstance(node, dict) or not isinstance(node.get("class_type"), str):
            raise ValueError("drawing_workflow_invalid")
        kind = node["class_type"]
        if kind == "LoadImage" and draft.bindings.image and node_id == draft.bindings.image.node_id:
            kind = "ImageReceiver"
        if kind == "PreviewImage" and node_id == draft.bindings.output.node_id:
            kind = "SaveImage"
        if kind not in _POLICIES:
            raise ValueError("drawing_node_unsupported")
        result.add(kind)
    return sorted(result)


class DrawingConfiguration:
    def __init__(self, repository, ai_repository, *, ai_manager=None, resource_factory=DrawingComfyClient,
                 gpu_queue=None, qq_status=None):
        self.repository, self.ai_repository, self.resource_factory = repository, ai_repository, resource_factory
        self.ai_manager = ai_manager
        self.gpu_queue = gpu_queue
        self.qq_status = qq_status
        self._wake, self._task = asyncio.Event(), None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def request_apply(self, _service_id=None):
        self._wake.set()

    async def _run(self):
        cursor = None
        while True:
            try:
                row = await asyncio.to_thread(self.repository.next_settings, cursor)
                if row is None and cursor is not None:
                    cursor = None
                    row = await asyncio.to_thread(self.repository.next_settings, None)
                if row is not None:
                    service_id, version = row
                    cursor = service_id
                    await self.apply(service_id, version)
            except Exception:
                logging.getLogger(__name__).warning("drawing_settings_apply_pending")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5)
            except TimeoutError:
                pass

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def apply(self, service_id: UUID, version: int) -> bool:
        settings = await asyncio.to_thread(self.repository.get_settings, service_id)
        if settings["row_version"] != version:
            return False
        if settings.get("applied_version") != version:
            if not await asyncio.to_thread(self.repository.mark_applied, service_id, version):
                return False
        if not settings["resource_configured"]:
            return True
        if settings["enabled"] and not await self.vision_ready(service_id):
            return True
        client = await self.client(service_id)
        try:
            state = await client.state()
            if state.get("adapter") != _V8:
                raise ValueError("drawing_resources_unsupported")
            await client.set_retention(service_id, version, settings["enabled"], settings["keep_drawing_warm"])
            await client.set_automatic(service_id, version, True)
        finally:
            await client.close()
        return await asyncio.to_thread(self.repository.mark_resource_applied, service_id, version)

    async def local_settings_ready(self, service_id: UUID) -> bool:
        settings = await asyncio.to_thread(self.repository.get_settings, service_id)
        return settings.get("resource_applied_version") == settings["row_version"]

    async def vision_ready(self, service_id: UUID) -> bool:
        if self.ai_repository is None:
            return True
        ai = await asyncio.to_thread(self.ai_repository.get_settings, service_id)
        provider = getattr(ai, 'providers', {}).get('vision')
        if (not getattr(ai, 'enabled', False) or not getattr(ai, 'vision_enabled', False)
                or provider is None or provider.adapter_kind != 'al1s_comfyui_vision'):
            return True
        if not ai.applied_version:
            return False
        if self.ai_manager is not None and await self.ai_manager.protect_vision(service_id, ai.applied_version):
            return True
        try:
            token = await asyncio.to_thread(self.repository.resource_credentials, service_id)
            if not token:
                return False
            original_endpoint = getattr(provider, "base_url", None) or "http://host.docker.internal:8188"
            endpoint = local_endpoint(original_endpoint)
            owner = "al1s-vision-" + hashlib.sha256((service_id.hex + "|" + original_endpoint).encode()).hexdigest()[:24]
            client = self.resource_factory(endpoint, owner, token)
            try:
                await client.set_pin(service_id, ai.applied_version, True)
            finally:
                await client.close()
            return True
        except (AttributeError, httpx.HTTPError, OSError, TimeoutError, ValueError):
            return False

    async def client(self, service_id: UUID, *, endpoint=None):
        token = await asyncio.to_thread(self.repository.resource_credentials, service_id)
        if not token:
            raise ValueError("drawing_resources_unavailable")
        if endpoint is None:
            settings = await asyncio.to_thread(self.repository.get_settings, service_id)
            endpoint = settings.get("comfy_base_url")
        endpoint = local_endpoint(endpoint)
        owner = "al1s-drawing-" + hashlib.sha256((service_id.hex + "|" + endpoint).encode()).hexdigest()[:24]
        return self.resource_factory(endpoint, owner, token)

    async def save_preset(self, service_id: UUID, draft: PresetDraft, aliases: dict, *, enabled: bool,
                          preset_id: UUID | None, expected_version: int, operation_id: str) -> dict:
        classes = _classes(draft)
        client = await self.client(service_id)
        try:
            state = await client.state()
            info = await client.node_info(classes)
            capabilities = build_capabilities(info, classes, adapter_version=state["adapter"])
            workflow = validate_workflow(draft, capabilities)
        finally:
            await client.close()
        result = await asyncio.to_thread(self.repository.save_preset, service_id, draft.name, workflow, aliases,
            enabled=enabled, preset_id=preset_id, expected_version=expected_version, operation_id=operation_id)
        return result.json_dict()

    async def check_preset(self, service_id: UUID, preset_id: UUID, expected_version: int, operation_id: str) -> dict:
        replay = await asyncio.to_thread(self.repository.check_receipt, service_id, preset_id, expected_version, operation_id)
        if replay is not None:
            return {"validation_state": replay}
        preset = await asyncio.to_thread(self.repository.get_preset, service_id, preset_id)
        if preset["row_version"] != expected_version:
            raise ValueError("drawing_preset_conflict")
        workflow = await asyncio.to_thread(self.repository.get_workflow, service_id, preset_id, preset["revision"])
        classes = sorted({node["class_type"] for node in workflow.graph.values()})
        assets = [{"kind": kind, "name": name} for kind, name in sorted(
            {(item.kind, item.name) for item in workflow.manifest})]
        client = await self.client(service_id)
        try:
            state = await client.state()
            result = await client.inspect(classes, assets)
            if result.get("adapter") != state["adapter"] or result.get("boot_id") != state["boot_id"]:
                raise ValueError("drawing_dependency_changed")
            result = compatible_inspection(result, workflow.adapter_version, classes)
        finally:
            await client.close()
        status = await asyncio.to_thread(self.repository.record_check, service_id, preset_id,
            preset["revision"], expected_version, result, operation_id=operation_id)
        return {"validation_state": status}

    async def resources(self, service_id: UUID) -> dict:
        settings = await asyncio.to_thread(self.repository.get_settings, service_id)
        usage = await asyncio.to_thread(self.repository.media_usage, service_id)
        queued = (self.gpu_queue.pending if self.gpu_queue is not None else
                  await asyncio.to_thread(self.repository.queued_count, 'comfy-local-8188'))
        gpu = {"gpu_busy": self.gpu_queue.active is not None} if self.gpu_queue is not None else {}
        gpu.update(usage)
        if self.qq_status is not None:
            gpu["qq_connection"] = (self.qq_status(service_id, settings.get("event_ws_url"))
                                    if settings.get("enabled") else "disabled")
        try:
            client = await self.client(service_id)
        except ValueError as error:
            if str(error) == "drawing_resources_unavailable":
                return {"state": "unconfigured", "queued": queued, **gpu}
            raise
        try:
            try:
                state = await client.state()
                return {"state": "available", "queued": queued, **state, **gpu}
            except (httpx.HTTPError, OSError, TimeoutError, ValueError):
                return {"state": "unavailable", "queued": queued, **gpu}
        finally:
            await client.close()

    async def release_idle(self, service_id: UUID) -> str:
        await asyncio.to_thread(self.repository.get_settings, service_id)
        client = await self.client(service_id)
        try:
            return await client.release_idle(service_id)
        finally:
            await client.close()
