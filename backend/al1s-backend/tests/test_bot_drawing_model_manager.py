import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from al1s.bot_ai.local_runtime import LocalAiRuntime
from al1s.bot_ai.model_manager import LocalModelManager
from al1s.bot_ai.configuration import ConfigController
from al1s.bot_drawing.configuration import DrawingConfiguration


def test_local_vision_uses_shared_resource_pin_and_disabling_releases_only_that_pin(tmp_path):
    async def run():
        service, identity, calls = uuid4(), uuid4(), []
        class Repo:
            def drawing_resource_credentials(self, _): return "a" * 48, True
        class History:
            def vision_jobs(self, *_): return []
            def save_vision_jobs(self, *_): pass
        class Resource:
            def __init__(self, endpoint, owner, token): calls.append(("new", endpoint, owner))
            async def state(self): return {"protocol": 1, "adapter": "v1", "boot_id": "boot"}
            async def set_pin(self, service_id, revision, enabled): calls.append(("pin", service_id, revision, enabled))
            async def close(self): calls.append(("closed",))
        class Client:
            async def close(self): pass
        runtime = LocalAiRuntime()
        manager = LocalModelManager(runtime, Repo(), History(), tmp_path,
                                    client_factory=lambda *_: Client(), resource_factory=Resource)
        profile = SimpleNamespace(id=identity, is_default=True, secret_configured=True,
            adapter_kind="openai_chat_completion", model_id="fixture", base_url="", supports_vision=False)
        provider = SimpleNamespace(adapter_kind="al1s_comfyui_vision", model_id="Qwen/Qwen3-VL-4B-Instruct",
            base_url="http://host.docker.internal:8188", configured=True)
        settings = SimpleNamespace(service_id=service, desired_version=1, enabled=True, providers={"vision": provider},
                                   vision_enabled=True, stt_enabled=False, tts_mode="off")
        staged = await manager.stage(settings, {}, chat_profiles=[profile], chat_secrets={str(identity): "key"})
        await manager.activate(settings, staged, await manager.binding(service))
        assert staged.comfy.resource is not None and ("pin", service, 1, True) in calls
        disabled = SimpleNamespace(service_id=service, desired_version=2, enabled=False)
        await manager.activate(disabled, None, await manager.binding(service))
        await manager.cleanup(service, 2)
        assert ("pin", service, 2, False) in calls
        await manager.close()
        await runtime.close()
    asyncio.run(run())


@pytest.mark.parametrize("offline_during", ["probe", "pin"])
def test_offline_comfy_does_not_block_chat_but_blocks_drawing_until_protected(tmp_path, offline_during):
    async def run():
        service, identity = uuid4(), uuid4()
        online = False
        class Repo:
            def drawing_resource_credentials(self, _): return "a" * 48, True
            def get_settings(self, _): return settings
            def provider_secrets(self, _): return {}
            def list_chat_profiles(self, _): return [profile]
            def chat_profile_secrets(self, _): return {str(identity): "key"}
            def mark_applied(self, *_): return True
            def mark_error(self, *_): raise AssertionError("configuration should be available")
            def cleanup_secrets(self, *_): return 0
        class History:
            def vision_jobs(self, *_): return []
            def save_vision_jobs(self, *_): pass
        class Resource:
            def __init__(self, *_): pass
            async def state(self):
                if not online and offline_during == "probe":
                    raise httpx.ConnectError("offline")
                return {"protocol": 1, "adapter": "v1", "boot_id": "boot"}
            async def set_pin(self, *_):
                if not online:
                    raise httpx.ConnectError("offline")
            async def close(self): pass
        class Client:
            async def close(self): pass
        class Sessions:
            resumed = False
            def pause(self, _): pass
            def resume(self, _): self.resumed = True
            async def wait_idle(self, _): return True
        runtime = LocalAiRuntime()
        repo = Repo()
        manager = LocalModelManager(runtime, repo, History(), tmp_path,
                                    client_factory=lambda *_: Client(), resource_factory=Resource)
        profile = SimpleNamespace(id=identity, is_default=True, secret_configured=True,
            adapter_kind="openai_chat_completion", model_id="fixture", base_url="", supports_vision=False)
        provider = SimpleNamespace(adapter_kind="al1s_comfyui_vision", model_id="Qwen/Qwen3-VL-4B-Instruct",
            base_url="http://host.docker.internal:8188", configured=True)
        settings = SimpleNamespace(service_id=service, desired_version=1, applied_version=1, enabled=True,
            providers={"vision": provider}, vision_enabled=True, stt_enabled=False, tts_mode="off")
        sessions = Sessions()
        controller = ConfigController(repo, manager, runtime, sessions)
        await controller.apply(service, 1)
        drawing = DrawingConfiguration(None, repo, ai_manager=manager)
        assert runtime.status(service)["connected"] and sessions.resumed
        assert manager._active[service].comfy.resource is not None
        assert not await drawing.vision_ready(service)
        online = True
        assert await drawing.vision_ready(service)
        await manager.close()
        await runtime.close()
    asyncio.run(run())


def test_local_vision_keeps_managed_resource_even_if_drawing_is_disabled(tmp_path):
    async def run():
        service = uuid4()
        class Repo:
            def drawing_resource_credentials(self, _): return "a" * 48, False
        class Resource:
            def __init__(self, *_): pass
            async def state(self): raise OSError("offline")
            async def close(self): pass
        manager = LocalModelManager(LocalAiRuntime(), Repo(), object(), tmp_path, resource_factory=Resource)
        record = SimpleNamespace(service_id=service)
        provider = SimpleNamespace(base_url="http://host.docker.internal:8188")
        assert isinstance(await manager._vision_resource(record, provider), Resource)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["missing_token", "unauthorized", "unsupported"])
def test_drawing_resource_configuration_errors_are_not_treated_as_offline(tmp_path, failure):
    async def run():
        class Repo:
            def drawing_resource_credentials(self, _):
                return (None if failure == "missing_token" else "a" * 48), True
        class Resource:
            def __init__(self, *_): pass
            async def state(self):
                if failure == "unauthorized":
                    request = httpx.Request("GET", "http://localhost:8188/")
                    raise httpx.HTTPStatusError("unauthorized", request=request,
                                                response=httpx.Response(401, request=request))
                raise ValueError("comfy_resource_unsupported")
            async def close(self): pass
        manager = LocalModelManager(LocalAiRuntime(), Repo(), object(), tmp_path, resource_factory=Resource)
        with pytest.raises(ValueError, match="ai_vision_resources_unavailable"):
            await manager._vision_resource(SimpleNamespace(service_id=uuid4()),
                                           SimpleNamespace(base_url="http://host.docker.internal:8188"))
    asyncio.run(run())
