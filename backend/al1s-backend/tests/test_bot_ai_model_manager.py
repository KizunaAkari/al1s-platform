import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from al1s.bot_ai.local_runtime import LocalAiRuntime
from al1s.bot_ai.model_manager import LocalModelManager
from al1s.bot_ai.configuration import ConfigController


class Client:
    def __init__(self): self.closed = False
    async def close(self): self.closed = True


def profile(identity, *, default=True, configured=True):
    return SimpleNamespace(id=identity, is_default=default, secret_configured=configured,
        adapter_kind='openai_chat_completion', model_id='fixture', base_url='',
        supports_vision=False, network_mode='direct')


def record(service, version, applied=0):
    return SimpleNamespace(service_id=service, desired_version=version, enabled=True,
        providers={}, vision_enabled=False, stt_enabled=False, tts_mode='off', applied_version=applied)


def test_configuration_switch_and_cleanup_keep_only_active_clients(tmp_path):
    async def run():
        service, identity, runtime = uuid4(), uuid4(), LocalAiRuntime()
        created = []
        def factory(_profile, _secret):
            created.append(Client())
            return created[-1]
        manager = LocalModelManager(runtime, object(), object(), tmp_path, client_factory=factory)
        first = await manager.stage(record(service, 1), {}, chat_profiles=[profile(identity)],
                                    chat_secrets={str(identity):'fixture-key'})
        binding = await manager.binding(service)
        await manager.activate(record(service, 1), first, binding)
        assert runtime.status(service)['bridge_version'] == 1
        second = await manager.stage(record(service, 2), {}, chat_profiles=[profile(identity)],
                                     chat_secrets={str(identity):'fixture-key'})
        binding = await manager.binding(service)
        await manager.activate(record(service, 2), second, binding)
        assert not created[0].closed
        await manager.cleanup(service, 2)
        assert created[0].closed and not created[1].closed
        await manager.close()
        assert created[1].closed
    asyncio.run(run())


def test_incomplete_stage_closes_clients_without_replacing_active_snapshot(tmp_path):
    async def run():
        service, first_id, second_id = uuid4(), uuid4(), uuid4()
        created = []
        def factory(_profile, _secret):
            created.append(Client())
            return created[-1]
        manager = LocalModelManager(LocalAiRuntime(), object(), object(), tmp_path, client_factory=factory)
        with pytest.raises(ValueError, match='provider'):
            await manager.stage(record(service, 1), {}, chat_profiles=[profile(first_id), profile(second_id, default=False)],
                                chat_secrets={str(first_id):'fixture-key'})
        assert len(created) == 1 and created[0].closed
    asyncio.run(run())


def test_already_applied_configuration_restarts_local_engine(tmp_path):
    async def run():
        service, identity, runtime = uuid4(), uuid4(), LocalAiRuntime()
        class Repo:
            def pending_settings(self): return []
            def applied_settings(self): return [(service, 1)]
            def get_settings(self, _service): return record(service, 1, applied=1)
            def provider_secrets(self, _service): return {}
            def list_chat_profiles(self, _service): return [profile(identity)]
            def chat_profile_secrets(self, _service): return {str(identity):'fixture-key'}
            def mark_applied(self, *_args): return True
            def cleanup_secrets(self, *_args): return 0
        class Sessions:
            def __init__(self): self.ready = asyncio.Event()
            def pause(self, _service): pass
            def resume(self, _service): self.ready.set()
            async def wait_idle(self, _service): return True
        sessions, repo = Sessions(), Repo()
        manager = LocalModelManager(runtime, repo, object(), tmp_path, client_factory=lambda *_:Client())
        controller = ConfigController(repo, manager, runtime, sessions)
        controller.start()
        await asyncio.wait_for(controller.wait_initial(), 2)
        assert sessions.ready.is_set()
        assert runtime.status(service)['bridge_version'] == 1
        await controller.close()
        await runtime.close()
    asyncio.run(run())


def test_failed_config_commit_restores_previous_runtime_and_closes_candidate(tmp_path):
    async def run():
        service, identity, runtime = uuid4(), uuid4(), LocalAiRuntime()
        created = []
        def factory(*_args):
            created.append(Client())
            return created[-1]
        class Repo:
            def get_settings(self, _service): return record(service, 2, applied=1)
            def provider_secrets(self, _service): return {}
            def list_chat_profiles(self, _service): return [profile(identity)]
            def chat_profile_secrets(self, _service): return {str(identity):'fixture-key'}
            def mark_applied(self, *_args): return False
            def mark_error(self, *_args): return True
        class Sessions:
            def pause(self, _service): pass
            def resume(self, _service): raise AssertionError('must remain paused')
            async def wait_idle(self, _service): return True
        repo = Repo()
        manager = LocalModelManager(runtime, repo, object(), tmp_path, client_factory=factory)
        initial = await manager.stage(record(service, 1), {}, chat_profiles=[profile(identity)],
                                      chat_secrets={str(identity):'fixture-key'})
        await manager.activate(record(service, 1), initial, await manager.binding(service))
        controller = ConfigController(repo, manager, runtime, Sessions())
        await controller.apply(service, 2)
        assert runtime.status(service)['bridge_version'] == 1
        assert not created[0].closed and created[1].closed
        await manager.close()
        await runtime.close()
    asyncio.run(run())


def test_background_imports_start_only_after_configuration_commit(tmp_path):
    async def run():
        service, identity, runtime = uuid4(), uuid4(), LocalAiRuntime()
        class Library:
            _jobs = []
            starts = 0
            def start_imports(self, *_args): self.starts += 1
            async def close(self): pass
        library = Library()
        manager = LocalModelManager(runtime, object(), object(), tmp_path,
                                    stickers=library, client_factory=lambda *_:Client())
        staged = await manager.stage(record(service, 1), {}, chat_profiles=[profile(identity)],
                                     chat_secrets={str(identity):'fixture-key'})
        await manager.activate(record(service, 1), staged, await manager.binding(service))
        assert library.starts == 0
        await manager.cleanup(service, 1)
        assert library.starts == 1
        await manager.close()
        await runtime.close()
    asyncio.run(run())


def test_accepted_sticker_import_drains_during_disable_without_bypassing_group_revocation(tmp_path):
    async def run():
        service = uuid4()
        settings = SimpleNamespace(enabled=False, desired_version=2, applied_version=1, groups=['123'])
        repository = SimpleNamespace(get_settings=lambda _: settings)
        manager = LocalModelManager(LocalAiRuntime(), repository, object(), tmp_path)
        manager._active[service] = SimpleNamespace(version=1)
        provider = f'al1s_{service.hex}_1_vision'
        assert await manager.authorize_import(service, '123', provider)
        settings.groups = []
        assert not await manager.authorize_import(service, '123', provider)
        settings.groups = ['123']
        settings.desired_version = settings.applied_version
        assert not await manager.authorize_import(service, '123', provider)
    asyncio.run(run())


def test_configuration_reports_pending_import_as_a_retryable_wait():
    async def run():
        service = uuid4()
        settings = record(service, 2, applied=1)
        settings.enabled = False
        errors = []
        class Repo:
            def get_settings(self, _): return settings
            def provider_secrets(self, _): return {}
            def mark_error(self, _service, _version, code): errors.append(code)
        class Engine:
            local_runtime = True
            async def binding(self, _): return {}
            async def activate(self, *_): raise ValueError('ai_sticker_queue_pending')
            async def restore(self, *_): pass
        class Sessions:
            def pause(self, _): pass
            async def wait_idle(self, _): return True
        hub = SimpleNamespace(status=lambda _: {'connected': True})
        controller = ConfigController(Repo(), Engine(), hub, Sessions())
        await controller.apply(service, 2)
        assert errors == ['ai_sticker_queue_pending']
    asyncio.run(run())


def test_stt_connection_test_handles_empty_transcription_without_claiming_accuracy(tmp_path):
    async def run():
        service, identity, runtime = uuid4(), uuid4(), LocalAiRuntime()
        async def transcription(**_kwargs): return SimpleNamespace(text='')
        def factory(spec, _secret):
            client = Client()
            if spec.adapter_kind == 'openai_whisper_api':
                client.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=transcription))
            return client
        settings = record(service, 1)
        settings.stt_enabled = True
        settings.providers = {'stt': SimpleNamespace(adapter_kind='openai_whisper_api',
            model_id='whisper-test', configured=True)}
        manager = LocalModelManager(runtime, object(), object(), tmp_path, client_factory=factory)
        staged = await manager.stage(settings, {'stt':'fixture-key'}, chat_profiles=[profile(identity)],
                                     chat_secrets={str(identity):'fixture-key'})
        await manager.activate(settings, staged, await manager.binding(service))
        result = await manager.test_provider(service, 1, 'stt', settings.providers['stt'])
        assert result['state'] == 'success' and '实际语音' in result['message']
        await manager.close()
        await runtime.close()
    asyncio.run(run())


def test_comfy_owner_preserves_existing_endpoint_spelling(tmp_path, monkeypatch):
    import hashlib
    from al1s.bot_ai import model_manager

    service = uuid4()
    captured = {}
    def fake_client(endpoint, owner, load, save):
        captured.update(endpoint=endpoint, owner=owner)
        return object()
    monkeypatch.setattr(model_manager, 'ComfyWorkflowClient', fake_client)
    manager = LocalModelManager(LocalAiRuntime(), object(), object(), tmp_path)
    spec = SimpleNamespace(base_url='http://host.docker.internal:8188/')
    manager._comfy(service, spec)
    expected = 'al1s-vision-' + hashlib.sha256((service.hex + '|' + spec.base_url).encode()).hexdigest()[:24]
    assert captured['owner'] == expected


def test_configuration_switch_waits_for_owned_comfy_cleanup(tmp_path):
    async def run():
        service, identity, runtime = uuid4(), uuid4(), LocalAiRuntime()
        class History:
            def vision_jobs(self, _service, _endpoint): return [{'prompt_id':str(uuid4())}]
        class Comfy:
            base_url = 'http://host.docker.internal:8188'
            async def cleanup(self): return None
        manager = LocalModelManager(runtime, object(), History(), tmp_path, client_factory=lambda *_:Client())
        old = await manager.stage(record(service, 1), {}, chat_profiles=[profile(identity)],
                                  chat_secrets={str(identity):'fixture-key'})
        await manager.activate(record(service, 1), old, await manager.binding(service))
        old.comfy = Comfy()
        candidate = await manager.stage(record(service, 2), {}, chat_profiles=[profile(identity)],
                                        chat_secrets={str(identity):'fixture-key'})
        with pytest.raises(ValueError, match='vision_cleanup_pending'):
            await manager.activate(record(service, 2), candidate, await manager.binding(service))
        assert runtime.status(service)['bridge_version'] == 1
        await manager.close()
        await runtime.close()
    asyncio.run(run())
