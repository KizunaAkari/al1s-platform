import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "comfy-resources"))
from al1s_resources.registry import ResourceRegistry
from al1s_resources.safety import check_files, read_output
from al1s_resources.inspection import AssetHashCache, inspect_assets
from al1s_resources.cache import OwnedCache
from al1s_resources.nodes import build_nodes
from al1s_resources.guard import JobContext, install_executor_guard, install_memory_guard
from al1s_resources.scheduler import ReleaseScheduler, install_queue_hook
from al1s_resources.routes import build_handlers
from al1s_resources.bootstrap import prepare_extension


def setup_registry():
    clock = [100.0]
    registry = ResourceRegistry(clock=lambda: clock[0], boot_id="boot-1")
    service, prompt = str(uuid4()), str(uuid4())
    registry.set_retention(service, revision=1, enabled=True, keep_warm=False)
    return registry, clock, service, prompt


def register(registry, service, prompt, operation="draw", digest="a" * 64):
    return registry.register(prompt, service, "owner", operation, digest, "boot-1")


def test_automatic_policy_releases_after_sixty_seconds_without_changing_legacy_flag():
    registry, clock, service, prompt = setup_registry()
    registry.set_retention(service, revision=2, enabled=True, keep_warm=True)
    registry.set_automatic(service, revision=2, enabled=True)
    register(registry, service, prompt)
    registry.begin(prompt, service, "owner", "draw", "a" * 64)
    registry.release(prompt, native_terminal=True)
    clock[0] += 59
    assert registry.due() is None
    assert registry.next_delay() == 1
    clock[0] += 1
    token = registry.due()
    assert token is not None
    register(registry, service, str(uuid4()))
    assert not registry.may_release(token, external_busy=False)


def test_automatic_policy_respects_other_connections_legacy_retention():
    registry, clock, service, prompt = setup_registry()
    registry.set_automatic(service, revision=1, enabled=True)
    other = str(uuid4())
    registry.set_retention(other, revision=1, enabled=True, keep_warm=True)
    register(registry, service, prompt)
    registry.release(prompt, native_terminal=False)
    clock[0] += 600
    assert registry.due() is None
    registry.set_retention(other, revision=2, enabled=False, keep_warm=True)
    assert registry.due() is not None


def test_automatic_policy_route_is_versioned_and_checks_the_retention_revision(tmp_path):
    import asyncio

    async def run():
        registry, _, service, _ = setup_registry()
        class Web:
            @staticmethod
            def json_response(value, status=200): return status, value
        class Scheduler:
            last_status = "idle"
            def refresh(self): pass
        class Request:
            headers = {"Authorization": "Bearer secret"}
            match_info = {"service": service}
            content_length = None
            async def json(self): return {"revision": 1, "enabled": True}
        handlers = build_handlers(registry, object(), Scheduler(), token="secret", web=Web,
                                  output_root=tmp_path, inspect_callback=lambda *_: {})
        status, state = await handlers["automatic"](Request())
        assert status == 200 and state["idle_timeout_seconds"] == 60
        registry.set_retention(service, revision=2, enabled=True, keep_warm=True)
        status, error = await handlers["automatic"](Request())
        assert status == 409 and error["error"] == "resource_revision_conflict"

    asyncio.run(run())


def test_switch_to_drawing_temporarily_releases_vision_without_changing_its_pin():
    registry, _, service, vision = setup_registry()
    registry.set_automatic(service, revision=1, enabled=True)
    registry.set_pin(service, revision=1, enabled=True)
    register(registry, service, vision, operation="vision")
    registry.begin(vision, service, "owner", "vision", "a" * 64)
    model = object()
    registry.remember(vision, "vision-model", model)
    registry.pin_resource(service, "vision-model")
    registry.release(vision, native_terminal=True)
    token = registry.request_switch("draw", "preset-1")
    freed = []
    assert registry.reclaim_switch(token, external_busy=False, release=lambda value: freed.append(value) or True) == "released"
    assert freed == [model] and registry.cached("vision-model") is None
    assert registry.state()["vision_pins"] == 1


def test_vision_state_reflects_actual_loaded_model_and_active_job():
    from types import SimpleNamespace

    registry, _, service, vision = setup_registry()
    registry.set_automatic(service, revision=1, enabled=True)
    registry.set_pin(service, revision=1, enabled=True)
    assert registry.state()["vision_state"] == "cold"
    register(registry, service, vision, operation="vision")
    registry.begin(vision, service, "owner", "vision", "a" * 64)
    model = SimpleNamespace(model=None)
    registry.remember(vision, "vision", (model,))
    registry.pin_resource(service, "vision")
    assert registry.state()["vision_state"] == "loading"
    model.model = object()
    assert registry.state()["vision_state"] == "loading"
    registry.release(vision, native_terminal=True)
    assert registry.state()["vision_state"] == "cold"
    model.al1s_ready = True
    assert registry.state()["vision_state"] == "warm"
    token = registry.request_switch("draw", "preset")
    registry.reclaim_switch(token, external_busy=False, release=lambda _: True)
    assert registry.state()["vision_state"] == "cold"


def test_switch_reuses_same_drawing_and_releases_it_for_vision_or_another_preset():
    registry, _, service, prompt = setup_registry()
    registry.set_automatic(service, revision=1, enabled=True)
    assert registry.request_switch("draw", "preset-1") is None
    register(registry, service, prompt)
    registry.begin(prompt, service, "owner", "draw", "a" * 64)
    model = object()
    registry.remember(prompt, "model", model)
    registry.release(prompt, native_terminal=True)
    assert registry.request_switch("draw", "preset-1") is None
    token = registry.request_switch("draw", "preset-2")
    freed = []
    assert registry.reclaim_switch(token, external_busy=False, release=lambda value: freed.append(value) or True) == "released"
    assert freed == [model]
    next_prompt = str(uuid4())
    register(registry, service, next_prompt)
    registry.begin(next_prompt, service, "owner", "draw", "a" * 64)
    registry.remember(next_prompt, "new-model", object())
    registry.release(next_prompt, native_terminal=True)
    token = registry.request_switch("vision", None)
    assert registry.reclaim_switch(token, external_busy=False, release=lambda _: True) == "released"
    assert registry.cached("new-model") is None


def test_switch_defers_for_active_or_external_resources_and_invalidates_stale_requests():
    registry, _, service, prompt = setup_registry()
    registry.set_automatic(service, revision=1, enabled=True)
    register(registry, service, prompt)
    registry.begin(prompt, service, "owner", "draw", "a" * 64)
    registry.remember(prompt, "owned", object())
    registry.remember(prompt, "external", object(), external=True)
    assert registry.request_switch("vision", None) == "deferred"
    registry.release(prompt, native_terminal=True)
    token = registry.request_switch("vision", None)
    assert registry.reclaim_switch(token, external_busy=True, release=lambda _: True) == "deferred"
    register(registry, service, str(uuid4()), operation="vision")
    assert registry.reclaim_switch(token, external_busy=False, release=lambda _: True) == "deferred"
    token = registry.request_switch("vision", None)
    assert registry.reclaim_switch(token, external_busy=False, release=lambda _: True) == "retained"
    assert registry.cached("external") is not None


def test_switch_route_reclaims_only_at_an_idle_native_queue(tmp_path):
    import asyncio
    import threading

    async def run():
        registry, _, service, vision = setup_registry()
        registry.set_automatic(service, revision=1, enabled=True)
        registry.set_pin(service, revision=1, enabled=True)
        register(registry, service, vision, operation="vision")
        registry.begin(vision, service, "owner", "vision", "a" * 64)
        registry.remember(vision, "vision", object())
        registry.pin_resource(service, "vision")
        registry.release(vision, native_terminal=True)
        class Queue:
            def __init__(self):
                self.mutex = threading.RLock()
                self.queue, self.currently_running, self.flags = [object()], {}, {}
            def set_flag(self, name, value): self.flags[name] = value
            def get_flags(self, reset=True): result, self.flags = self.flags, {}; return result
        class Cache:
            def release(self, prompt): return True
        class Scheduler:
            last_status = "idle"
            def refresh(self): pass
            def retry(self): pass
        class Web:
            @staticmethod
            def json_response(value, status=200): return status, value
        class Request:
            headers = {"Authorization": "Bearer secret"}
            content_length = None
            async def json(self): return {"operation": "draw", "profile": "preset-1"}
        queue, cache, scheduler = Queue(), Cache(), Scheduler()
        install_queue_hook(queue, registry, cache, scheduler)
        handlers = build_handlers(registry, queue, scheduler, token="secret", web=Web,
                                  output_root=tmp_path, inspect_callback=lambda *_: {}, cache=cache)
        assert (await handlers["switch"](Request()))[1]["status"] == "deferred"
        queue.queue.clear()
        assert (await handlers["switch"](Request()))[1]["status"] == "queued"
        queue.get_flags()
        assert registry.cached("vision") is None and registry.state()["vision_pins"] == 1
        assert (await handlers["switch"](Request()))[1]["status"] == "ready"

    asyncio.run(run())


def test_cancel_queued_does_not_interrupt_a_running_native_task(tmp_path):
    import asyncio
    import threading

    async def run():
        registry, _, service, prompt = setup_registry()
        register(registry, service, prompt, operation="vision")
        registry.begin(prompt, service, "owner", "vision", "a" * 64)
        item = (1, prompt, {}, {"client_id": "owner"})
        class Queue:
            mutex = threading.RLock()
            queue = []
            currently_running = {1: item}
            def delete_queue_item(self, _predicate): pytest.fail("running task is not queued")
            def interrupt_if_running(self, _prompt): pytest.fail("must never interrupt a running task")
            def get_history(self, **_): return {}
        class Scheduler:
            last_status = "idle"
            def refresh(self): pass
        class Web:
            @staticmethod
            def json_response(value, status=200): return status, value
        class Request:
            headers = {"Authorization": "Bearer secret"}
            match_info = {"prompt": prompt}
        handlers = build_handlers(registry, Queue(), Scheduler(), token="secret", web=Web,
                                  output_root=tmp_path, inspect_callback=lambda *_: {})
        assert (await handlers["cancel_queued"](Request()))[1]["status"] == "running"
        assert registry.get(prompt)["state"] == "started"
    asyncio.run(run())


def test_cancel_queued_removes_only_the_owned_pending_task(tmp_path):
    import asyncio
    import threading

    async def run():
        registry, _, service, prompt = setup_registry()
        register(registry, service, prompt, operation="vision")
        registry.begin(prompt, service, "owner", "vision", "a" * 64)
        item = (1, prompt, {}, {"client_id": "owner"})
        class Queue:
            mutex = threading.RLock()
            queue = [item]
            currently_running = {}
            def delete_queue_item(self, predicate):
                self.queue = [row for row in self.queue if not predicate(row)]
                return True
            def get_history(self, **_): return {}
        class Scheduler:
            last_status = "idle"
            def refresh(self): pass
        class Web:
            @staticmethod
            def json_response(value, status=200): return status, value
        class Request:
            headers = {"Authorization": "Bearer secret"}
            match_info = {"prompt": prompt}
        queue = Queue()
        handlers = build_handlers(registry, queue, Scheduler(), token="secret", web=Web,
                                  output_root=tmp_path, inspect_callback=lambda *_: {})
        assert (await handlers["cancel_queued"](Request()))[1]["status"] == "cancelled"
        assert not queue.queue and registry.get(prompt)["state"] == "dequeued"
    asyncio.run(run())


def test_registration_is_idempotent_and_begin_is_one_time():
    registry, _, service, prompt = setup_registry()
    assert register(registry, service, prompt)["state"] == "registered"
    assert register(registry, service, prompt)["state"] == "registered"
    with pytest.raises(ValueError, match="resource_job_conflict"):
        register(registry, service, prompt, digest="b" * 64)
    assert registry.begin(prompt, service, "owner", "draw", "a" * 64) is True
    assert registry.begin(prompt, service, "owner", "draw", "a" * 64) is False
    with pytest.raises(ValueError, match="resource_job_conflict"):
        registry.begin(prompt, service, "other", "draw", "a" * 64)


def test_on_demand_waits_600_seconds_and_new_drawing_invalidates_old_request():
    registry, clock, service, prompt = setup_registry()
    register(registry, service, prompt)
    registry.begin(prompt, service, "owner", "draw", "a" * 64)
    assert registry.release(prompt, native_terminal=False) == "deferred"
    assert registry.release(prompt, native_terminal=True) == "retained"
    clock[0] += 599
    assert registry.due() is None
    clock[0] += 1
    token = registry.due()
    assert token is not None
    register(registry, service, str(uuid4()))
    assert registry.may_release(token, external_busy=False) is False


def test_resident_mode_and_other_connections_keep_models_warm():
    registry, clock, service, prompt = setup_registry()
    second = str(uuid4())
    registry.set_retention(second, revision=1, enabled=True, keep_warm=True)
    register(registry, service, prompt)
    registry.release(prompt, native_terminal=False)
    clock[0] += 1000
    assert registry.due() is None
    registry.set_retention(second, revision=2, enabled=False, keep_warm=True)
    assert registry.due() is not None


def test_tag_does_not_reset_drawing_idle_but_protects_active_resource():
    registry, clock, service, prompt = setup_registry()
    register(registry, service, prompt)
    registry.release(prompt, native_terminal=False)
    clock[0] += 599
    tag = str(uuid4())
    register(registry, service, tag, operation="tag")
    clock[0] += 1
    token = registry.due()
    assert token is not None and not registry.may_release(token, external_busy=False)
    registry.release(tag, native_terminal=False)
    assert registry.may_release(token, external_busy=False)


def test_release_rechecks_external_activity_and_config_versions():
    registry, clock, service, prompt = setup_registry()
    register(registry, service, prompt)
    registry.release(prompt, native_terminal=False)
    clock[0] += 600
    token = registry.due()
    assert not registry.may_release(token, external_busy=True)
    registry.set_retention(service, revision=2, enabled=True, keep_warm=True)
    assert not registry.may_release(token, external_busy=False)
    with pytest.raises(ValueError, match="resource_revision_conflict"):
        registry.set_retention(service, revision=1, enabled=True, keep_warm=False)


def test_reclaim_keeps_vision_and_its_shared_parent_but_releases_unused_lora():
    registry, clock, service, prompt = setup_registry()
    registry.set_pin(service, revision=1, enabled=True)
    register(registry, service, prompt)
    registry.begin(prompt, service, "owner", "draw", "a" * 64)
    base, lora = object(), object()
    assert registry.remember(prompt, "base", base) is base
    assert registry.remember(prompt, "lora", lora, parents=("base",)) is lora
    registry.pin_resource(service, "base")
    registry.release(prompt, native_terminal=True)
    clock[0] += 600
    freed = []
    result = registry.reclaim(registry.due(), external_busy=False, release=lambda value: freed.append(value) or True)
    assert result == "retained" and freed == [lora] and registry.cached("base") is base
    registry.set_pin(service, revision=2, enabled=False)
    result = registry.reclaim(registry.due(), external_busy=False, release=lambda value: freed.append(value) or True)
    assert result == "released" and freed == [lora, base]


def test_reclaim_never_releases_external_or_unknown_resources():
    registry, clock, service, prompt = setup_registry()
    register(registry, service, prompt)
    registry.begin(prompt, service, "owner", "draw", "a" * 64)
    external = object()
    registry.remember(prompt, "external", external, external=True)
    registry.release(prompt, native_terminal=True)
    clock[0] += 600
    result = registry.reclaim(registry.due(), external_busy=False,
                              release=lambda _: pytest.fail("external object must remain untouched"))
    assert result == "retained" and registry.cached("external") is external


def test_manual_release_keeps_another_connections_models_and_vision():
    registry, _, service, prompt = setup_registry()
    other, other_prompt, vision = str(uuid4()), str(uuid4()), str(uuid4())
    registry.set_retention(service, revision=2, enabled=True, keep_warm=True)
    registry.set_retention(other, revision=1, enabled=True, keep_warm=True)
    register(registry, service, prompt)
    registry.begin(prompt, service, 'owner', 'draw', 'a' * 64)
    own = object(); registry.remember(prompt, 'own', own)
    registry.release(prompt, native_terminal=True)
    register(registry, other, other_prompt)
    registry.begin(other_prompt, other, 'owner', 'draw', 'a' * 64)
    shared = object(); registry.remember(other_prompt, 'other', shared)
    registry.release(other_prompt, native_terminal=True)
    registry.register(vision, service, 'owner', 'vision', 'b' * 64, 'boot-1')
    registry.begin(vision, service, 'owner', 'vision', 'b' * 64)
    protected = object(); registry.remember(vision, 'vision', protected)
    registry.release(vision, native_terminal=True)
    token = registry.request_manual_release(service)
    freed = []
    assert registry.reclaim(token, external_busy=False, release=lambda value: freed.append(value) or True) == 'retained'
    assert freed == [own] and registry.cached('other') is shared and registry.cached('vision') is protected


def test_manual_release_is_invalidated_by_a_new_task():
    registry, _, service, prompt = setup_registry()
    register(registry, service, prompt)
    registry.release(prompt, native_terminal=False)
    token = registry.request_manual_release(service)
    register(registry, service, str(uuid4()))
    assert not registry.may_release(token, external_busy=False)


def test_manual_release_route_only_schedules_when_native_queue_is_idle(tmp_path):
    import asyncio
    import threading
    async def run():
        registry, _, service, _ = setup_registry()
        class Queue:
            def __init__(self):
                self.mutex = threading.RLock(); self.queue = [object()]; self.currently_running = {}; self.flags = {}
            def set_flag(self, key, value): self.flags[key] = value
        class Scheduler: last_status = 'idle'
        class Web:
            @staticmethod
            def json_response(value, status=200): return status, value
        class Request:
            headers = {'Authorization': 'Bearer secret'}; match_info = {'service': service}
        queue, scheduler = Queue(), Scheduler()
        handlers = build_handlers(registry, queue, scheduler, token='secret', web=Web,
                                  output_root=tmp_path, inspect_callback=lambda *_: {})
        assert (await handlers['release_idle'](Request()))[1]['status'] == 'deferred' and not queue.flags
        queue.queue.clear()
        assert (await handlers['release_idle'](Request()))[1]['status'] == 'queued'
        assert queue.flags['al1s_idle_release'][2] == service
    asyncio.run(run())


def test_runtime_source_fingerprint_must_match_exactly(tmp_path):
    import hashlib
    (tmp_path / "nodes.py").write_bytes(b"known")
    expected = {"nodes.py": hashlib.sha256(b"known").hexdigest()}
    check_files(tmp_path, expected)
    (tmp_path / "nodes.py").write_bytes(b"changed")
    with pytest.raises(ValueError, match="resource_version_unsupported"):
        check_files(tmp_path, expected)


def test_output_reader_refuses_symlinks_and_other_task_directories(tmp_path):
    prompt = str(uuid4())
    directory = tmp_path / "al1s-drawing" / prompt
    directory.mkdir(parents=True)
    file = directory / "output_00001_.png"
    file.write_bytes(b"known")
    assert read_output(tmp_path, prompt, file.name, max_bytes=10) == b"known"
    with pytest.raises(ValueError, match="resource_output_invalid"):
        read_output(tmp_path, prompt, "../secret.png", max_bytes=10)
    file.unlink()
    try:
        file.symlink_to(tmp_path / "outside")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="resource_output_invalid"):
        read_output(tmp_path, prompt, file.name, max_bytes=10)


def test_inspection_uses_registered_model_directories_and_detects_changed_asset(tmp_path):
    import hashlib
    root = tmp_path / "checkpoints"
    root.mkdir()
    model = root / "model.safetensors"
    model.write_bytes(b"model-v1")
    class Paths:
        @staticmethod
        def get_folder_paths(kind):
            return [str(root)] if kind == "checkpoints" else []
        @staticmethod
        def get_full_path(kind, name):
            return str(root / name) if kind == "checkpoints" else None
    asset = {"kind": "checkpoints", "name": "model.safetensors"}
    first = inspect_assets(Paths, [asset])[0]
    assert first["sha256"] == hashlib.sha256(b"model-v1").hexdigest()
    model.write_bytes(b"model-v2")
    assert inspect_assets(Paths, [asset])[0]["sha256"] != first["sha256"]
    with pytest.raises(ValueError, match="resource_asset_invalid"):
        inspect_assets(Paths, [{"kind": "checkpoints", "name": "../secret"}])


def test_inspection_accepts_a_valid_model_when_another_registered_directory_is_missing(tmp_path):
    root = tmp_path / 'models'
    root.mkdir()
    model = root / 'model.safetensors'
    model.write_bytes(b'model')
    class Paths:
        @staticmethod
        def get_folder_paths(_kind): return [str(root), str(tmp_path / 'unused-output')]
        @staticmethod
        def get_full_path(_kind, name): return str(root / name)
    assert inspect_assets(Paths, [{'kind': 'checkpoints', 'name': model.name}])[0]['size'] == 5


def test_unchanged_model_reuses_digest_and_replacement_forces_rehash(tmp_path, monkeypatch):
    import hashlib
    root = tmp_path / "models"
    root.mkdir()
    model = root / "model.safetensors"
    model.write_bytes(b"first")
    class Paths:
        @staticmethod
        def get_folder_paths(_): return [str(root)]
        @staticmethod
        def get_full_path(_, name): return str(root / name)
    assets, cache = [{"kind": "checkpoints", "name": model.name}], AssetHashCache()
    first = inspect_assets(Paths, assets, cache=cache)
    with monkeypatch.context() as patch:
        patch.setattr(hashlib, "sha256", lambda *_: pytest.fail("unchanged file should use cached digest"))
        assert inspect_assets(Paths, assets, cache=cache) == first
    model.write_bytes(b"second version")
    assert inspect_assets(Paths, assets, cache=cache) != first


def test_only_cache_entries_with_our_exact_prompt_are_removed():
    class KeySet:
        keys = {}
    class Cache:
        def __init__(self):
            self.cache_key_set = KeySet()
            self.cache = {}
            self.subcaches = {}
    cache = Cache()
    prompt = str(uuid4())
    owned = frozenset({("al1s_prompt_id", prompt), ("node", "loader")})
    unrelated = frozenset({("al1s_prompt_id", str(uuid4())), ("node", "loader")})
    value, other = object(), object()
    cache.cache_key_set.keys = {"1": owned, "2": unrelated}
    cache.cache = {owned: value, unrelated: other}
    tracker = OwnedCache()
    tracker.capture(prompt, cache)
    assert tracker.release(prompt) is True
    assert owned not in cache.cache and cache.cache[unrelated] is other


def test_cache_tracker_recognizes_comfy_input_signature_shape():
    from al1s_resources.cache import _contains_marker
    prompt = str(uuid4())
    signature = frozenset({(0, frozenset({
        (0, 'AL1SCheckpointLoader'), (1, 'changed'),
        (2, frozenset({(0, 'al1s_prompt_id'), (1, prompt)})),
    }))})
    assert _contains_marker(signature, prompt)
    assert not _contains_marker(signature, str(uuid4()))


def test_cache_entry_changed_by_another_owner_is_never_removed():
    class Cache:
        pass
    cache, prompt = Cache(), str(uuid4())
    key = frozenset({("al1s_prompt_id", prompt)})
    cache.cache_key_set = type("Keys", (), {"keys": {"1": key}})()
    cache.cache, cache.subcaches = {key: object()}, {}
    tracker = OwnedCache()
    tracker.capture(prompt, cache)
    replacement = object()
    cache.cache[key] = replacement
    assert tracker.release(prompt) is False and cache.cache[key] is replacement


def test_completed_task_cache_is_released_even_when_drawing_models_stay_resident():
    import threading
    registry, _, service, prompt = setup_registry()
    registry.set_retention(service, revision=2, enabled=True, keep_warm=True)
    register(registry, service, prompt)
    registry.begin(prompt, service, 'owner', 'draw', 'a' * 64)
    model = object()
    registry.remember(prompt, 'model', model)
    key = frozenset({('al1s_prompt_id', prompt)})
    class KeySet:
        keys = {'1': key}
    class Cache:
        cache_key_set = KeySet()
        cache = {key: object()}
        subcaches = {}
    class Queue:
        def __init__(self):
            self.mutex = threading.RLock(); self.queue = []; self.currently_running = {}; self.flags = {}
        def set_flag(self, name, value): self.flags[name] = value
        def get_flags(self, reset=True):
            flags = dict(self.flags)
            if reset: self.flags.clear()
            return flags
    queue, native_cache, tracker = Queue(), Cache(), OwnedCache()
    tracker.capture(prompt, native_cache)
    registry.release(prompt, native_terminal=True)
    scheduler = ReleaseScheduler(registry, queue, cache=tracker)
    install_queue_hook(queue, registry, tracker, scheduler)
    scheduler.refresh()
    assert 'al1s_cache_release' in queue.flags
    queue.get_flags()
    assert key not in native_cache.cache and not tracker.pending(prompt)
    assert registry.cached('model') is model and registry.due() is None
    scheduler.close()


def test_release_remains_deferred_until_owned_cache_is_cleared(tmp_path):
    import asyncio
    import threading
    async def run():
        registry, _, service, prompt = setup_registry()
        register(registry, service, prompt)
        registry.begin(prompt, service, 'owner', 'draw', 'a' * 64)
        key = frozenset({('al1s_prompt_id', prompt)})
        class KeySet: keys = {'1': key}
        class Cache:
            cache_key_set = KeySet(); cache = {key: object()}; subcaches = {}
        class Queue:
            def __init__(self):
                self.mutex = threading.RLock(); self.queue = []; self.currently_running = {}; self.flags = {}
            def get_history(self, prompt_id=None):
                return {prompt: {'prompt': (0, prompt, {}, {'client_id': 'owner'}, []),
                                 'status': {'status_str': 'success'}}}
            def set_flag(self, name, value): self.flags[name] = value
            def get_flags(self, reset=True):
                flags = dict(self.flags)
                if reset: self.flags.clear()
                return flags
        class Web:
            @staticmethod
            def json_response(value, status=200): return status, value
        class Request:
            headers = {'Authorization': 'Bearer secret'}; match_info = {'prompt': prompt}
        queue, native_cache, tracker = Queue(), Cache(), OwnedCache()
        tracker.capture(prompt, native_cache)
        scheduler = ReleaseScheduler(registry, queue, cache=tracker)
        install_queue_hook(queue, registry, tracker, scheduler)
        handlers = build_handlers(registry, queue, scheduler, token='secret', web=Web, cache=tracker,
                                  output_root=tmp_path, inspect_callback=lambda *_: {})
        assert (await handlers['release'](Request()))[1]['status'] == 'deferred'
        queue.get_flags()
        assert (await handlers['release'](Request()))[1]['status'] == 'retained'
        scheduler.close()
    asyncio.run(run())


def test_release_removes_only_terminal_owned_comfy_output(tmp_path):
    import asyncio
    import threading

    async def run():
        registry, _, service, prompt = setup_registry()
        register(registry, service, prompt)
        registry.begin(prompt, service, 'owner', 'draw', 'a' * 64)
        directory = tmp_path / 'al1s-drawing' / prompt
        directory.mkdir(parents=True)
        image = directory / 'output_00001_.png'
        image.write_bytes(b'owned preview')
        other = directory / 'user-file.txt'
        other.write_bytes(b'keep')
        foreign = tmp_path / 'al1s-drawing' / str(uuid4())
        foreign.mkdir()
        (foreign / 'output_00001_.png').write_bytes(b'foreign')
        output = {'9': {'images': [{'filename': image.name,
            'subfolder': f'al1s-drawing\\{prompt}', 'type': 'output'}]}}
        class Queue:
            mutex = threading.RLock()
            queue = []
            currently_running = {}
            def get_history(self, prompt_id=None):
                return {prompt: {'prompt': (0, prompt, {}, {'client_id': 'owner'}, []),
                    'status': {'status_str': 'success'}, 'outputs': output}}
        class Scheduler:
            def refresh(self): pass
        class Web:
            @staticmethod
            def json_response(value, status=200): return status, value
        class Request:
            headers = {'Authorization': 'Bearer secret'}
            match_info = {'prompt': prompt}
        handlers = build_handlers(registry, Queue(), Scheduler(), token='secret', web=Web,
                                  output_root=tmp_path, inspect_callback=lambda *_: {})
        assert (await handlers['release'](Request()))[1]['status'] == 'retained'
        assert not image.exists() and other.read_bytes() == b'keep'
        assert (foreign / 'output_00001_.png').read_bytes() == b'foreign'
        assert (await handlers['release'](Request()))[1]['status'] == 'retained'
    asyncio.run(run())


def test_release_does_not_delete_unowned_or_unconfirmed_comfy_output(tmp_path):
    import asyncio
    import threading

    async def run():
        registry, _, service, prompt = setup_registry()
        register(registry, service, prompt)
        registry.begin(prompt, service, 'owner', 'draw', 'a' * 64)
        directory = tmp_path / 'al1s-drawing' / prompt
        directory.mkdir(parents=True)
        image = directory / 'output_00001_.png'
        image.write_bytes(b'keep')
        class Queue:
            mutex = threading.RLock()
            queue = []
            currently_running = {}
            owner = 'other'
            subfolder = f'al1s-drawing/{prompt}'
            def get_history(self, prompt_id=None):
                return {prompt: {'prompt': (0, prompt, {}, {'client_id': self.owner}, []),
                    'status': {'status_str': 'success'}, 'outputs': {'9': {'images': [
                        {'filename': image.name, 'subfolder': self.subfolder, 'type': 'output'}]}}}}
        class Scheduler:
            def refresh(self): pass
        class Web:
            @staticmethod
            def json_response(value, status=200): return status, value
        class Request:
            headers = {'Authorization': 'Bearer secret'}
            match_info = {'prompt': prompt}
        queue = Queue()
        handlers = build_handlers(registry, queue, Scheduler(), token='secret', web=Web,
                                  output_root=tmp_path, inspect_callback=lambda *_: {})
        assert (await handlers['release'](Request()))[1]['status'] == 'deferred'
        assert image.read_bytes() == b'keep' and registry.get(prompt)['state'] == 'started'
        queue.owner = 'owner'
        queue.subfolder = f'al1s-drawing/../{prompt}'
        status, value = await handlers['release'](Request())
        assert status == 409 and value['error'] == 'resource_output_invalid'
        assert image.read_bytes() == b'keep' and registry.get(prompt)['state'] == 'started'
        queue.subfolder = f'al1s-drawing/{prompt}'
        unstarted, _, second_service, _ = setup_registry()
        register(unstarted, second_service, prompt)
        handlers = build_handlers(unstarted, queue, Scheduler(), token='secret', web=Web,
                                  output_root=tmp_path, inspect_callback=lambda *_: {})
        assert (await handlers['release'](Request()))[1]['status'] == 'deferred'
        assert image.read_bytes() == b'keep' and unstarted.get(prompt)['state'] == 'registered'
    asyncio.run(run())


def test_managed_loader_reuses_only_registered_asset_and_does_not_modify_base():
    calls = []
    class Checkpoint:
        RETURN_TYPES = ("MODEL", "CLIP", "VAE")
        @classmethod
        def INPUT_TYPES(cls): return {"required": {"ckpt_name": ("STRING",)}}
        def load_checkpoint(self, ckpt_name):
            calls.append(ckpt_name)
            return (object(), object(), object())
    class Lora:
        RETURN_TYPES = ("MODEL", "CLIP")
        @classmethod
        def INPUT_TYPES(cls): return {"required": {"model": ("MODEL",), "clip": ("CLIP",),
            "lora_name": ("STRING",), "strength_model": ("FLOAT",), "strength_clip": ("FLOAT",)}}
        def load_lora(self, model, clip, lora_name, strength_model, strength_clip):
            return (object(), object())
    registry, _, service, prompt = setup_registry()
    assets = [{"kind": "checkpoints", "name": "model", "size": 1, "sha256": "b" * 64},
              {"kind": "loras", "name": "lora", "size": 1, "sha256": "c" * 64}]
    registry.register(prompt, service, "owner", "draw", "a" * 64, "boot-1", assets)
    registry.begin(prompt, service, "owner", "draw", "a" * 64)
    nodes = build_nodes(registry, {"CheckpointLoaderSimple": Checkpoint, "LoraLoader": Lora},
                        current_job=lambda: (prompt, "a" * 64))
    checkpoint = nodes["AL1SCheckpointLoader"]()
    base = checkpoint.process("model", prompt)
    assert checkpoint.process("model", prompt) is base and calls == ["model"]
    lora = nodes["AL1SLoraLoader"]().process(base[0], base[1], "lora", 0.8, 0.7, prompt)
    assert lora[0] is not base[0] and lora[1] is not base[1]
    with pytest.raises(ValueError, match="resource_asset_unregistered"):
        checkpoint.process("other", prompt)
    with pytest.raises(ValueError, match="resource_job_conflict"):
        checkpoint.process("model", str(uuid4()))
    changed = build_nodes(registry, {"CheckpointLoaderSimple": Checkpoint},
        current_job=lambda: (prompt, "a" * 64), verify_asset=lambda *_: "d" * 64)
    with pytest.raises(ValueError, match="resource_asset_changed"):
        changed["AL1SCheckpointLoader"]().process("model", prompt)


def test_moss_split_loaders_and_model_clones_are_owned_and_keep_vision_protected():
    from types import SimpleNamespace

    calls = []

    class UNet:
        RETURN_TYPES = ("MODEL",)
        @classmethod
        def INPUT_TYPES(cls): return {"required": {"unet_name": ("STRING",), "weight_dtype": ("STRING",)}}
        def load_unet(self, name, dtype):
            calls.append(("unet", name, dtype))
            return (object(),)

    class Clip:
        RETURN_TYPES = ("CLIP",)
        @classmethod
        def INPUT_TYPES(cls): return {"required": {"clip_name": ("STRING",), "type": ("STRING",)}}
        def load_clip(self, name, kind, device="default"):
            calls.append(("clip", name, kind, device))
            return (object(),)

    class Vae:
        RETURN_TYPES = ("VAE",)
        @classmethod
        def INPUT_TYPES(cls): return {"required": {"vae_name": ("STRING",)}}
        def load_vae(self, name):
            calls.append(("vae", name))
            return (object(),)

    class Lora:
        RETURN_TYPES = ("MODEL",)
        @classmethod
        def INPUT_TYPES(cls): return {"required": {"model": ("MODEL",), "lora_name": ("STRING",),
                                                   "strength_model": ("FLOAT",)}}
        def load_lora_model_only(self, model, name, strength):
            calls.append(("lora", name, strength))
            return (object(),)

    class Skip:
        @classmethod
        def execute(cls, model, start_percent, end_percent, double_layers="", single_layers=""):
            calls.append(("skip", single_layers))
            return SimpleNamespace(result=(object(),))

    class Tsr:
        @classmethod
        def execute(cls, model, tsr_k, tsr_sigma):
            calls.append(("tsr", tsr_k, tsr_sigma))
            return SimpleNamespace(result=(object(),))

    registry, clock, service, prompt = setup_registry()
    registry.set_pin(service, revision=1, enabled=True)
    vision = str(uuid4())
    register(registry, service, vision, "vision")
    registry.begin(vision, service, "owner", "vision", "a" * 64)
    vision_model = (object(),)
    registry.remember(vision, "vision-model", vision_model)
    registry.pin_resource(service, "vision-model")
    registry.release(vision, native_terminal=True)
    assets = [{"kind": kind, "name": name, "size": 1, "sha256": digest * 64}
              for kind, name, digest in (("diffusion_models", "moss", "b"),
                                         ("text_encoders", "qwen", "c"),
                                         ("vae", "vae", "d"), ("loras", "style", "e"))]
    registry.register(prompt, service, "owner", "draw", "a" * 64, "boot-1", assets)
    registry.begin(prompt, service, "owner", "draw", "a" * 64)
    native = {"UNETLoader": UNet, "CLIPLoader": Clip, "VAELoader": Vae, "LoraLoaderModelOnly": Lora,
              "SkipLayerGuidanceDiTSimple": Skip, "TemporalScoreRescaling": Tsr}
    wrappers = build_nodes(registry, native, current_job=lambda: (prompt, "a" * 64))
    unet = wrappers["AL1SUNETLoader"]()
    base = unet.process("moss", "default", prompt)
    assert unet.process("moss", "default", prompt) is base
    clip = wrappers["AL1SCLIPLoader"]().process("qwen", "moss_image", prompt)
    vae = wrappers["AL1SVAELoader"]().process("vae", prompt)
    lora = wrappers["AL1SLoraLoaderModelOnly"]().process(base[0], "style", 0.7, prompt)
    skip = wrappers["AL1SSkipLayerGuidanceDiTSimple"]().process(lora[0], "", "22, 24", 0.0, 0.5, prompt)
    tsr = wrappers["AL1STemporalScoreRescaling"]().process(skip[0], 0.95, 1.0, prompt)
    for value in (base[0], clip[0], vae[0], lora[0], skip[0], tsr[0]):
        assert registry.owns_value(value)
    assert calls.count(("unet", "moss", "default")) == 1
    with pytest.raises(ValueError, match="resource_asset_unregistered"):
        unet.process("other", "default", prompt)
    registry.release(prompt, native_terminal=True)
    clock[0] += 601
    freed = []
    assert registry.reclaim(registry.due(), external_busy=False,
                            release=lambda value: freed.append(value) or True) == "retained"
    assert vision_model not in freed and registry.cached("vision-model") is vision_model
    assert all(value in freed for value in (base, clip, vae, lora, skip, tsr))


def test_qwen_image_encoder_is_cached_by_type_and_rejects_unapproved_types():
    calls = []

    class Clip:
        RETURN_TYPES = ("CLIP",)
        @classmethod
        def INPUT_TYPES(cls): return {"required": {"clip_name": ("STRING",), "type": ("STRING",)}}
        def load_clip(self, name, kind, device="default"):
            calls.append((name, kind, device))
            return (object(),)

    registry, _, service, prompt = setup_registry()
    assets = [{"kind": "text_encoders", "name": "encoder", "size": 1, "sha256": "c" * 64}]
    registry.register(prompt, service, "owner", "draw", "a" * 64, "boot-1", assets)
    registry.begin(prompt, service, "owner", "draw", "a" * 64)
    wrappers = build_nodes(registry, {"CLIPLoader": Clip}, current_job=lambda: (prompt, "a" * 64))
    loader = wrappers["AL1SCLIPLoader"]()
    result = loader.process("encoder", "qwen_image", prompt)
    assert loader.process("encoder", "qwen_image", prompt) is result
    assert registry.owns_value(result[0])
    assert calls == [("encoder", "qwen_image", "default")]
    with pytest.raises(ValueError, match="resource_model_type_invalid"):
        loader.process("encoder", "flux2", prompt)


def test_qwen_edit_node_inspection_checks_the_native_file(tmp_path, monkeypatch):
    import hashlib
    from al1s_resources import inspection, safety

    node = type("TextEncodeQwenImage21", (), {})
    path = tmp_path / "comfy_extras/nodes_qwen.py"
    path.parent.mkdir(parents=True)
    path.write_text("qwen edit")
    monkeypatch.setitem(safety.CORE_HASHES, "comfy_extras/nodes_qwen.py", hashlib.sha256(b"qwen edit").hexdigest())
    monkeypatch.setattr(inspection.inspect, "getsourcefile", lambda value: str(path))
    result = inspection.inspect_dependencies(tmp_path, {"TextEncodeQwenImage21": node}, None,
                                             ["TextEncodeQwenImage21"], [])
    assert result["nodes"][0]["name"] == "TextEncodeQwenImage21"
    path.write_text("changed")
    with pytest.raises(ValueError, match="resource_version_unsupported"):
        inspection.inspect_dependencies(tmp_path, {"TextEncodeQwenImage21": node}, None,
                                        ["TextEncodeQwenImage21"], [])


def test_moss_inspection_checks_native_origins_and_hashes(tmp_path, monkeypatch):
    import hashlib
    from al1s_resources import inspection, safety

    paths = {"UNETLoader": "nodes.py", "CLIPLoader": "nodes.py", "VAELoader": "nodes.py",
             "LoraLoaderModelOnly": "nodes.py", "EmptyFlux2LatentImage": "comfy_extras/nodes_flux.py",
             "SkipLayerGuidanceDiTSimple": "comfy_extras/nodes_slg.py",
             "TemporalScoreRescaling": "comfy_extras/nodes_eps.py"}
    native = {name: type(name, (), {}) for name in paths}
    origins = {native[name]: str(tmp_path / path) for name, path in paths.items()}
    for path in set(paths.values()):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(path)
        monkeypatch.setitem(safety.CORE_HASHES, path, hashlib.sha256(path.encode()).hexdigest())
    monkeypatch.setattr(inspection.inspect, "getsourcefile", lambda node: origins[node])
    result = inspection.inspect_dependencies(tmp_path, native, None, list(paths), [])
    assert {node["name"] for node in result["nodes"]} == set(paths)
    (tmp_path / "comfy_extras/nodes_flux.py").write_text("changed")
    with pytest.raises(ValueError, match="resource_version_unsupported"):
        inspection.inspect_dependencies(tmp_path, native, None, list(paths), [])


def test_memory_guard_protects_managed_models_from_manual_execution_and_external_models_from_managed_execution():
    class Model:
        def __init__(self, base): self.model = base
        def is_clone(self, other): return self.model is other.model
    class Loaded:
        def __init__(self, model): self.model = model
    class Memory:
        def __init__(self): self.current_loaded_models = []; self.kept = None; self.loads = 0
        def free_memory(self, amount, device, keep_loaded=None, **kwargs):
            self.kept = keep_loaded
        def load_models_gpu(self, models, **kwargs): self.loads += 1
    registry, _, service, prompt = setup_registry()
    register(registry, service, prompt)
    registry.begin(prompt, service, "owner", "draw", "a" * 64)
    own = Model(object())
    registry.remember(prompt, "owned", (own,))
    external = Model(object())
    memory = Memory()
    loaded_own, loaded_external = Loaded(own), Loaded(external)
    memory.current_loaded_models = [loaded_own, loaded_external]
    current = [None]
    install_memory_guard(memory, registry, current_job=lambda: current[0])
    memory.free_memory(1, "gpu", keep_loaded=[])
    assert memory.kept == [loaded_own]
    with pytest.raises(ValueError, match="resource_managed_clone_conflict"):
        memory.load_models_gpu([Model(own.model)])
    current[0] = (prompt, "a" * 64)
    memory.free_memory(1, "gpu", keep_loaded=[])
    assert loaded_external in memory.kept and loaded_own in memory.kept
    with pytest.raises(ValueError, match="resource_external_clone_conflict"):
        memory.load_models_gpu([Model(external.model)])
    assert memory.loads == 0


def test_vision_node_reuses_its_owned_instance_while_pin_is_enabled(monkeypatch):
    from al1s_resources import nodes as managed_nodes
    loads = []
    class Vision:
        RETURN_TYPES = ("STRING",)
        @classmethod
        def INPUT_TYPES(cls): return {"required": {"model_name": ("STRING",)}}
        def process(self, **inputs):
            loads.append(self)
            return ("description",)
    monkeypatch.setattr(managed_nodes, "_check_vision_assets", lambda *_: None)
    registry, _, service, prompt = setup_registry()
    registry.set_pin(service, revision=1, enabled=True)
    registry.register(prompt, service, "owner", "vision", "a" * 64, "boot-1")
    registry.begin(prompt, service, "owner", "vision", "a" * 64)
    nodes = build_nodes(registry, {"AILab_QwenVL_Advanced": Vision},
                        current_job=lambda: (prompt, "a" * 64), folder_paths=object())
    managed = nodes["AL1SQwenVLAdvanced"]()
    inputs = {"model_name": "Qwen3-VL-4B-Instruct", "quantization": "4-bit (VRAM-friendly)",
              "attention_mode": "sdpa", "use_torch_compile": False, "device": "auto", "keep_model_loaded": True}
    assert managed.process(al1s_prompt_id=prompt, **inputs) == ("description",)
    assert managed.process(al1s_prompt_id=prompt, **inputs) == ("description",)
    assert loads[0] is loads[1]


def test_turning_off_vision_releases_its_model_without_releasing_warm_drawing():
    registry, _, service, drawing = setup_registry()
    registry.set_retention(service, revision=2, enabled=True, keep_warm=True)
    registry.set_pin(service, revision=1, enabled=True)
    register(registry, service, drawing)
    registry.begin(drawing, service, "owner", "draw", "a" * 64)
    draw_model, vision_model = object(), object()
    registry.remember(drawing, "drawing", draw_model)
    registry.release(drawing, native_terminal=True)
    vision = str(uuid4())
    registry.register(vision, service, "owner", "vision", "b" * 64, "boot-1")
    registry.begin(vision, service, "owner", "vision", "b" * 64)
    registry.remember(vision, "vision", vision_model)
    registry.pin_resource(service, "vision")
    registry.release(vision, native_terminal=True)
    registry.set_pin(service, revision=2, enabled=False)
    token = registry.vision_due()
    assert token is not None
    freed = []
    assert registry.reclaim_vision(token, external_busy=False, release=lambda value: freed.append(value) or True) == "released"
    assert freed == [vision_model] and registry.cached("drawing") is draw_model


def test_vision_release_wakes_worker_without_clearing_drawing_cache():
    import threading
    registry, _, service, drawing = setup_registry()
    registry.set_retention(service, revision=2, enabled=True, keep_warm=True)
    registry.set_pin(service, revision=1, enabled=True)
    register(registry, service, drawing)
    registry.begin(drawing, service, "owner", "draw", "a" * 64)
    registry.remember(drawing, "drawing", object())
    registry.release(drawing, native_terminal=True)
    vision = str(uuid4())
    registry.register(vision, service, "owner", "vision", "b" * 64, "boot-1")
    registry.begin(vision, service, "owner", "vision", "b" * 64)
    registry.remember(vision, "vision", object())
    registry.pin_resource(service, "vision")
    registry.release(vision, native_terminal=True)
    registry.set_pin(service, revision=2, enabled=False)
    class Queue:
        def __init__(self):
            self.mutex, self.queue, self.currently_running, self.flags = threading.RLock(), [], {}, {}
        def set_flag(self, name, value): self.flags[name] = value
        def get_flags(self, reset=True): result, self.flags = self.flags, {}; return result
    class Cache:
        def __init__(self): self.released = []
        def release(self, prompt): self.released.append(prompt); return True
    queue, cache = Queue(), Cache()
    scheduler = ReleaseScheduler(registry, queue)
    install_queue_hook(queue, registry, cache, scheduler)
    scheduler.refresh()
    queue.get_flags()
    assert cache.released == [vision] and registry.cached("drawing") is not None
    scheduler.close()


def test_executor_validates_graph_and_owner_before_managed_nodes_run():
    import hashlib
    import json
    registry, _, service, prompt = setup_registry()
    graph = {"1": {"class_type": "AL1SCheckpointLoader", "inputs": {"al1s_prompt_id": prompt}}}
    digest = hashlib.sha256(json.dumps(graph, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    registry.register(prompt, service, "owner", "draw", digest, "boot-1")
    registry.begin(prompt, service, "owner", "draw", digest)
    context, seen = JobContext(), []
    class Cache:
        cache = {}
        cache_key_set = type("Keys", (), {"keys": {}})()
        subcaches = {}
    class Executor:
        def __init__(self): self.caches = type("Caches", (), {"outputs": Cache()})()
        def execute(self, prompt, prompt_id, extra_data=None, execute_outputs=None):
            seen.append((prompt, context.current()))
    execution = type("Execution", (), {"PromptExecutor": Executor})
    install_executor_guard(execution, registry, OwnedCache(), context)
    executor = Executor()
    executor.execute(graph, prompt, {"client_id": "owner"}, ["1"])
    assert seen[-1][0] == graph and seen[-1][1] == (prompt, digest)
    executor.execute({"changed": {}}, prompt, {"client_id": "owner"}, ["1"])
    assert seen[-1][0]["0"]["class_type"] == "AL1SInvalidRequest"
    assert context.current() is None


def test_timer_wakes_worker_at_deadline_and_preserves_native_flags():
    import threading
    registry, clock, service, prompt = setup_registry()
    register(registry, service, prompt)
    registry.release(prompt, native_terminal=False)
    timers = []
    class Timer:
        def __init__(self, delay, callback): self.delay, self.callback, self.daemon = delay, callback, False
        def start(self): timers.append(self)
        def cancel(self): pass
    class Queue:
        def __init__(self):
            self.mutex = threading.RLock()
            self.queue, self.currently_running, self.flags = [], {}, {"native_flag": "kept"}
        def set_flag(self, name, value): self.flags[name] = value
        def get_flags(self, reset=True):
            result = dict(self.flags)
            if reset: self.flags.clear()
            return result
    queue = Queue()
    scheduler = ReleaseScheduler(registry, queue, timer_factory=Timer)
    install_queue_hook(queue, registry, OwnedCache(), scheduler)
    scheduler.refresh()
    assert timers[-1].delay == 600
    clock[0] += 600
    timers[-1].callback()
    assert "al1s_idle_release" in queue.flags
    flags = queue.get_flags()
    assert flags["native_flag"] == "kept" and "al1s_idle_release" not in flags
    assert registry.due() is None


def test_worker_rechecks_manual_queue_before_reclaim():
    import threading
    registry, clock, service, prompt = setup_registry()
    register(registry, service, prompt)
    registry.release(prompt, native_terminal=False)
    clock[0] += 600
    class Queue:
        def __init__(self):
            self.mutex = threading.RLock()
            self.queue, self.currently_running = ["manual task"], {}
            self.flags = {"al1s_idle_release": registry.due()}
        def set_flag(self, name, value): self.flags[name] = value
        def get_flags(self, reset=True): result, self.flags = self.flags, {}; return result
    class Scheduler:
        def __init__(self): self.retries = 0
        def retry(self): self.retries += 1
    queue, scheduler = Queue(), Scheduler()
    install_queue_hook(queue, registry, OwnedCache(), scheduler)
    queue.get_flags()
    assert scheduler.retries == 1 and registry.due() is not None


def test_resource_routes_require_token_and_cancel_only_owned_queued_job(tmp_path):
    import asyncio
    import threading
    async def run():
        registry, _, service, prompt = setup_registry()
        register(registry, service, prompt)
        registry.begin(prompt, service, "owner", "draw", "a" * 64)
        class Queue:
            def __init__(self):
                self.mutex = threading.RLock()
                self.queue = [(0, prompt, {}, {"client_id": "owner"}, [])]
                self.currently_running = {}
            def get_history(self, prompt_id=None): return {}
            def delete_queue_item(self, predicate):
                for item in list(self.queue):
                    if predicate(item): self.queue.remove(item); return True
                return False
            def interrupt_if_running(self, _): return False
        class Scheduler:
            def refresh(self): pass
        class Web:
            @staticmethod
            def json_response(value, status=200): return status, value
        class Request:
            content_length = 0
            query = {}
            def __init__(self, token):
                self.headers = {"Authorization": token}
                self.match_info = {"prompt": prompt}
            async def json(self): return {}
        queue = Queue()
        handlers = build_handlers(registry, queue, Scheduler(), token="secret", web=Web,
                                  output_root=tmp_path, inspect_callback=lambda *_: {})
        assert (await handlers["cancel"](Request("Bearer wrong")))[0] == 401
        assert len(queue.queue) == 1
        status, result = await handlers["cancel"](Request("Bearer secret"))
        assert status == 200 and result["status"] == "cancelled" and queue.queue == []
        status, result = await handlers["release"](Request("Bearer secret"))
        assert status == 200 and result["status"] == "retained"
    asyncio.run(run())


def test_resource_routes_fail_closed_until_runtime_hooks_are_ready(tmp_path):
    import asyncio
    async def run():
        registry, _, _, _ = setup_registry()
        class Web:
            @staticmethod
            def json_response(value, status=200): return status, value
        class Request:
            headers = {"Authorization": "Bearer secret"}
        handlers = build_handlers(registry, object(), object(), token="secret", web=Web,
                                  output_root=tmp_path, inspect_callback=lambda *_: {}, ready=lambda: False)
        status, result = await handlers["state"](Request())
        assert status == 503 and result["error"] == "resource_unavailable"
    asyncio.run(run())


def test_explicit_rejection_only_releases_a_job_absent_from_native_queue_and_history(tmp_path):
    import asyncio
    import threading
    async def run():
        registry, _, service, prompt = setup_registry()
        register(registry, service, prompt)
        registry.begin(prompt, service, 'owner', 'draw', 'a' * 64)
        class Queue:
            mutex = threading.RLock()
            queue = [(0, prompt, {}, {'client_id': 'owner'}, [])]
            currently_running = {}
            def get_history(self, prompt_id=None): return {}
        class Scheduler:
            def refresh(self): pass
        class Web:
            @staticmethod
            def json_response(value, status=200): return status, value
        class Request:
            headers = {'Authorization': 'Bearer secret'}
            match_info = {'prompt': prompt}
        queue = Queue()
        handlers = build_handlers(registry, queue, Scheduler(), token='secret', web=Web,
                                  output_root=tmp_path, inspect_callback=lambda *_: {})
        assert (await handlers['reject'](Request()))[1]['status'] == 'deferred'
        assert registry.get(prompt)['state'] == 'started'
        queue.queue.clear()
        assert (await handlers['reject'](Request()))[1]['status'] == 'rejected'
        assert (await handlers['reject'](Request()))[1]['status'] == 'rejected'
        assert registry.get(prompt)['state'] == 'dequeued'
    asyncio.run(run())


def test_missing_required_nodes_does_not_install_runtime_hooks(tmp_path):
    import asyncio
    import threading
    async def run():
        class Routes:
            def __getattr__(self, _): return lambda _path: lambda function: function
        class App:
            on_startup, on_cleanup = [], []
        class Queue:
            mutex = threading.RLock()
            queue, currently_running = [], {}
        class Server:
            routes, app, prompt_queue = Routes(), App(), Queue()
        class Native:
            NODE_CLASS_MAPPINGS = {}
        class Execution:
            class PromptExecutor:
                def execute(self, *_args): pass
        class Memory:
            def free_memory(self, *_args, **_kwargs): pass
            def load_models_gpu(self, *_args, **_kwargs): pass
        class Paths:
            @staticmethod
            def get_output_directory(): return str(tmp_path)
        class Web:
            @staticmethod
            def json_response(value, status=200): return status, value
        old = Execution.PromptExecutor.execute
        state = prepare_extension(Server(), Native, Execution, Memory(), Paths, tmp_path, "a" * 48,
                                  web=Web, preflight=lambda _: None)
        await Server.app.on_startup[-1](Server.app)
        assert state["ready"] is False and Execution.PromptExecutor.execute is old
    asyncio.run(run())
