import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from al1s.bot_drawing.capabilities import _POLICIES
from al1s.bot_drawing.configuration import DrawingConfiguration
from al1s.bot_drawing.contracts import InputBindings, OutputBinding, PortBinding, PresetDraft


class ResourceRepo:
    def resource_credentials(self, _): return "a" * 48
    def get_settings(self, _): return {"comfy_base_url": "http://host.docker.internal:8188"}
    def media_usage(self, _): return {"media_used_bytes": 0, "media_limit_bytes": 1024 ** 3}


def test_drawing_resource_client_uses_its_own_address_without_reading_chat():
    async def run():
        captured = []

        class Repo(ResourceRepo):
            def get_settings(self, _): return {"comfy_base_url": "http://127.0.0.1:8188"}

        class Chat:
            def get_settings(self, _): raise AssertionError("drawing must not read chat address")

        def factory(endpoint, owner, token):
            captured.append((endpoint, token))
            return object()

        await DrawingConfiguration(Repo(), Chat(), resource_factory=factory).client(uuid4())
        assert captured == [("http://127.0.0.1:8188", "a" * 48)]

    asyncio.run(run())


def source_graph():
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": ""}},
        "3": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "4": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "seed": 1, "steps": 20,
            "cfg": 7.0, "sampler_name": "euler", "scheduler": "normal", "positive": ["2", 0],
            "negative": ["2", 0], "latent_image": ["3", 0], "denoise": 1.0}},
        "5": {"class_type": "VAEDecode", "inputs": {"samples": ["4", 0], "vae": ["1", 2]}},
        "6": {"class_type": "SaveImage", "inputs": {"images": ["5", 0], "filename_prefix": "original"}},
    }


def draft():
    return PresetDraft(name="测试", kind="txt2img", graph=source_graph(), bindings=InputBindings(
        positive=PortBinding(node_id="2", input_name="text"), seed=PortBinding(node_id="4", input_name="seed"),
        output=OutputBinding(node_id="6")))


def object_info(names):
    result = {}
    for name in names:
        module, output, fields = _POLICIES[name]
        result[name] = {"python_module": module, "output": list(output), "output_node": name == "SaveImage",
            "input": {"required": {key: [spec[0]] for key, spec in fields.items()}}}
    return result


def test_save_and_check_use_live_node_capabilities_and_asset_signatures():
    async def run():
        service, preset_id = uuid4(), uuid4()
        class Repo(ResourceRepo):
            workflow = None
            def save_preset(self, _service, name, workflow, aliases, **kwargs):
                self.workflow = workflow
                return SimpleNamespace(json_dict=lambda: {"id": str(preset_id), "number": 1, "revision": 1,
                    "row_version": 1, "enabled": True, "kind": "txt2img"})
            def get_workflow(self, *_): return self.workflow
            def get_preset(self, *_): return {"revision": 1, "row_version": 1}
            def check_receipt(self, *_): return None
            def record_check(self, _service, _preset, revision, version, dependencies, **_kwargs):
                assert revision == version == 1 and dependencies["assets"][0]["name"] == "sdxl.safetensors"
                return "needs_preview"
        class Ai:
            def drawing_resource_credentials(self, _): return "a" * 48, False
            def get_settings(self, _): return SimpleNamespace(providers={})
        class Resource:
            def __init__(self, *_): pass
            async def state(self): return {"adapter": "test-v1", "boot_id": "boot-1"}
            async def node_info(self, names): return object_info(names)
            async def inspect(self, names, assets):
                return {"adapter": "test-v1", "boot_id": "boot-1",
                    "nodes": [{"name": name, "sha256": "b" * 64} for name in names],
                    "assets": [{**item, "size": 100, "sha256": "c" * 64} for item in assets]}
            async def close(self): pass
        repo = Repo()
        config = DrawingConfiguration(repo, Ai(), resource_factory=Resource)
        result = await config.save_preset(service, draft(), {}, enabled=True, preset_id=None,
                                          expected_version=0, operation_id="one")
        assert result["id"] == str(preset_id)
        assert await config.check_preset(service, preset_id, 1, "check-1") == {"validation_state": "needs_preview"}
    asyncio.run(run())


def test_v6_resource_rechecks_legacy_preset_without_changing_its_adapter_identity():
    async def run():
        v5 = "comfy-0.43-bb878b9-al1s-v5"
        v6 = "comfy-0.43-bb878b9-al1s-v6"
        class Repo(ResourceRepo):
            def check_receipt(self, *_): return None
            def get_preset(self, *_): return {"revision": 1, "row_version": 1}
            def get_workflow(self, *_):
                return SimpleNamespace(graph=source_graph(), adapter_version=v5, manifest=[])
            def record_check(self, _service, _preset, _revision, _version, dependencies, **_):
                assert dependencies["adapter"] == v5
                return "ready"
        class Ai:
            def drawing_resource_credentials(self, _): return "a" * 48, False
            def get_settings(self, _): return SimpleNamespace(providers={})
        class Resource:
            def __init__(self, *_): pass
            async def state(self): return {"adapter": v6, "boot_id": "boot"}
            async def inspect(self, names, _assets):
                return {"adapter": v6, "boot_id": "boot", "nodes": names, "assets": []}
            async def close(self): pass
        config = DrawingConfiguration(Repo(), Ai(), resource_factory=Resource)
        assert await config.check_preset(uuid4(), uuid4(), 1, "check") == {"validation_state": "ready"}
    asyncio.run(run())


def test_missing_resource_token_never_submits_or_silently_validates():
    async def run():
        class Ai:
            def drawing_resource_credentials(self, _): return None, False
        config = DrawingConfiguration(SimpleNamespace(resource_credentials=lambda _: None), Ai())
        with pytest.raises(ValueError, match="drawing_resources_unavailable"):
            await config.save_preset(uuid4(), draft(), {}, enabled=True, preset_id=None,
                                     expected_version=0, operation_id="one")
    asyncio.run(run())


def test_common_and_resource_versions_apply_independently():
    async def run():
        service, calls = uuid4(), []
        class Repo(ResourceRepo):
            def get_settings(self, _): return {"row_version": 3, "enabled": True,
                "keep_drawing_warm": True, "resource_configured": True}
            def mark_applied(self, identity, version): calls.append(("applied", identity, version)); return True
            def mark_resource_applied(self, identity, version): calls.append(("resource_applied", identity, version)); return True
        class Ai:
            def drawing_resource_credentials(self, _): return "a" * 48, True
            def get_settings(self, _): return SimpleNamespace(providers={})
        class Resource:
            def __init__(self, *_): pass
            async def state(self): return {"adapter": "comfy-0.43-bb878b9-al1s-v8", "boot_id": "boot-1"}
            async def set_retention(self, identity, version, enabled, keep_warm):
                calls.append(("retention", identity, version, enabled, keep_warm))
            async def set_automatic(self, identity, version, enabled):
                calls.append(("automatic", identity, version, enabled))
            async def close(self): pass
        config = DrawingConfiguration(Repo(), Ai(), resource_factory=Resource)
        assert await config.apply(service, 3)
        assert calls == [("applied", service, 3), ("retention", service, 3, True, True),
                         ("automatic", service, 3, True), ("resource_applied", service, 3)]
    asyncio.run(run())


def test_new_resource_policy_does_not_apply_on_a_legacy_resource_adapter():
    async def run():
        class Repo(ResourceRepo):
            def get_settings(self, _): return {"row_version": 1, "enabled": True,
                "keep_drawing_warm": False, "resource_configured": True}
            def mark_applied(self, *_): return True
            def mark_resource_applied(self, *_): pytest.fail("legacy adapter must not be marked resource applied")
        class Ai:
            def drawing_resource_credentials(self, _): return "a" * 48, True
            def get_settings(self, _): return SimpleNamespace(providers={})
        class Resource:
            def __init__(self, *_): pass
            async def state(self): return {"adapter": "comfy-0.43-bb878b9-al1s-v7", "boot_id": "boot"}
            async def close(self): pass
        config = DrawingConfiguration(Repo(), Ai(), resource_factory=Resource)
        with pytest.raises(ValueError, match="drawing_resources_unsupported"):
            await config.apply(uuid4(), 1)
    asyncio.run(run())


def test_failed_residency_sync_does_not_claim_resource_applied():
    async def run():
        service = uuid4()
        class Repo(ResourceRepo):
            def get_settings(self, _): return {"row_version": 1, "enabled": True,
                "keep_drawing_warm": True, "resource_configured": True}
            def mark_applied(self, *_): return True
            def mark_resource_applied(self, *_): pytest.fail("must not mark resource applied")
        class Ai:
            def drawing_resource_credentials(self, _): return "a" * 48, True
            def get_settings(self, _): return SimpleNamespace(providers={})
        class Resource:
            def __init__(self, *_): pass
            async def state(self): raise OSError("offline")
            async def close(self): pass
        config = DrawingConfiguration(Repo(), Ai(), resource_factory=Resource)
        with pytest.raises(OSError):
            await config.apply(service, 1)
    asyncio.run(run())


def test_resource_status_keeps_queue_count_when_comfy_is_unavailable():
    async def run():
        class Repo(ResourceRepo):
            def get_settings(self, _): return {}
            def queued_count(self, endpoint):
                assert endpoint == 'comfy-local-8188'
                return 2
        class Ai:
            def drawing_resource_credentials(self, _): return 'a' * 48, True
            def get_settings(self, _): return SimpleNamespace(providers={})
        class Resource:
            def __init__(self, *_): pass
            async def state(self): raise OSError('offline')
            async def close(self): pass
        result = await DrawingConfiguration(Repo(), Ai(), resource_factory=Resource).resources(uuid4())
        assert result == {'state': 'unavailable', 'queued': 2, 'media_used_bytes': 0, 'media_limit_bytes': 1024 ** 3}
    asyncio.run(run())


def test_resource_status_reports_the_shared_gpu_queue_not_only_drawing_rows():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        queue = GpuQueue()
        queue.reserve("vision:one")
        queue.reserve("draw-two")
        class Repo(ResourceRepo):
            def get_settings(self, _): return {}
        class Ai:
            def drawing_resource_credentials(self, _): return 'a' * 48, True
            def get_settings(self, _): return SimpleNamespace(providers={})
        class Resource:
            def __init__(self, *_): pass
            async def state(self): return {"adapter": "v8", "boot_id": "boot"}
            async def close(self): pass
        config = DrawingConfiguration(Repo(), Ai(), resource_factory=Resource, gpu_queue=queue)
        result = await config.resources(uuid4())
        assert result["queued"] == 1 and result["gpu_busy"] is True
    asyncio.run(run())


def test_drawing_cannot_apply_until_existing_local_vision_is_managed():
    async def run():
        service, calls = uuid4(), []
        class Repo(ResourceRepo):
            def get_settings(self, _): return {'row_version': 3, 'enabled': True,
                'keep_drawing_warm': False, 'resource_configured': True}
            def mark_applied(self, *_): calls.append('applied'); return True
            def mark_resource_applied(self, *_): calls.append('resource_applied'); return True
        class Ai:
            def get_settings(self, _): return SimpleNamespace(enabled=True, vision_enabled=True,
                desired_version=7, applied_version=7,
                providers={'vision': SimpleNamespace(adapter_kind='al1s_comfyui_vision', base_url='')})
            def drawing_resource_credentials(self, _): return 'a' * 48, True
        class Manager:
            ready = False
            async def protect_vision(self, identity, version):
                assert identity == service and version == 7
                return self.ready
        class Resource:
            def __init__(self, *_): pass
            async def state(self): return {'adapter': 'comfy-0.43-bb878b9-al1s-v8', 'boot_id': 'boot'}
            async def set_retention(self, *_): calls.append('retention')
            async def set_automatic(self, *_): calls.append('automatic')
            async def close(self): pass
        manager = Manager()
        config = DrawingConfiguration(Repo(), Ai(), ai_manager=manager, resource_factory=Resource)
        assert await config.apply(service, 3) and calls == ['applied']
        manager.ready = True
        assert await config.apply(service, 3)
        assert calls == ['applied', 'applied', 'retention', 'automatic', 'resource_applied']
    asyncio.run(run())


def test_drawing_checks_applied_vision_protection_instead_of_chat_sync_state():
    async def run():
        service, calls = uuid4(), []

        class Ai:
            def get_settings(self, _):
                return SimpleNamespace(enabled=True, vision_enabled=True, desired_version=8, applied_version=7,
                    providers={'vision': SimpleNamespace(adapter_kind='al1s_comfyui_vision')})

        class Manager:
            async def protect_vision(self, identity, version):
                calls.append((identity, version))
                return True

        config = DrawingConfiguration(ResourceRepo(), Ai(), ai_manager=Manager())
        assert await config.vision_ready(service)
        assert calls == [(service, 7)]

    asyncio.run(run())


def test_local_vision_can_be_protected_when_chat_controller_has_no_active_snapshot():
    async def run():
        service, calls = uuid4(), []

        class Ai:
            def get_settings(self, _):
                return SimpleNamespace(enabled=True, vision_enabled=True, desired_version=8, applied_version=7,
                    providers={'vision': SimpleNamespace(adapter_kind='al1s_comfyui_vision')})

        class Resource:
            def __init__(self, *_): pass
            async def set_pin(self, identity, version, enabled): calls.append((identity, version, enabled))
            async def close(self): pass

        config = DrawingConfiguration(ResourceRepo(), Ai(), resource_factory=Resource)
        assert await config.vision_ready(service)
        assert calls == [(service, 7, True)]

    asyncio.run(run())


def test_local_vision_fallback_protects_vision_address_not_drawing_address():
    async def run():
        endpoints = []

        class DrawingRepo(ResourceRepo):
            def get_settings(self, _): return {'comfy_base_url': 'http://host.docker.internal:8188'}

        class Ai:
            def get_settings(self, _):
                return SimpleNamespace(enabled=True, vision_enabled=True, desired_version=7, applied_version=7,
                    providers={'vision': SimpleNamespace(adapter_kind='al1s_comfyui_vision',
                                                          base_url='http://127.0.0.1:8188')})

        class Resource:
            def __init__(self, endpoint, *_): endpoints.append(endpoint)
            async def set_pin(self, *_): pass
            async def close(self): pass

        config = DrawingConfiguration(DrawingRepo(), Ai(), resource_factory=Resource)
        assert await config.vision_ready(uuid4())
        assert endpoints == ['http://127.0.0.1:8188']

    asyncio.run(run())
