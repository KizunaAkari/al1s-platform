from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import asyncio
import httpx

from al1s.api.bot_drawing import router


def test_online_preset_defaults_to_automatic_size_and_quality():
    from al1s.api.bot_drawing import OnlinePresetWrite
    value = OnlinePresetWrite(name='自动绘图', model_id='gpt-image-2.6-flare')
    assert value.size == 'auto' and value.quality == 'auto'
    explicit = OnlinePresetWrite(name='固定绘图', model_id='gpt-image-2.6-flare',
                                 size='1024x1536', quality='high')
    assert explicit.size == '1024x1536' and explicit.quality == 'high'


def test_task_image_response_uses_saved_output_type():
    from types import SimpleNamespace
    from al1s.api.bot_drawing import task_image

    class Repo:
        def preview_media(self, _service, _task): return self.reference, "a" * 64
    class Media:
        def read(self, _reference, *, expected_sha256):
            assert expected_sha256 == "a" * 64
            return b"test-only-media"
    repo = Repo()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(drawing_repository=repo, drawing_media=Media())))
    for name, expected in (("output.png", "image/png"), ("output.gif", "image/gif")):
        repo.reference = "al1s-drawing/00000000-0000-0000-0000-000000000001/" + name
        response = asyncio.run(task_image(uuid4(), uuid4(), request))
        assert response.media_type == expected and response.body == b"test-only-media"


def app_for_tests():
    app = FastAPI()
    class Repo:
        def get_settings(self, service):
            return {"service_id": str(service), "enabled": False, "keep_drawing_warm": False,
                    "default_text_preset_id": None, "default_edit_preset_id": None,
                    "row_version": 0, "applied_version": 0, "resource_configured": False}
        def list_group_modes(self, _): return [{"group_id": "123", "mode": "green", "row_version": 0}]
        def list_presets(self, _): return []
        def list_tasks(self, _, **_kwargs): return {"items": [], "next_after_id": None}
        def save_settings(self, service, **values):
            assert values["expected_version"] == 0 and values["operation_id"] == "operation-1"
            return type("Settings", (), {"json_dict": lambda _: {"row_version": 1}})()
    class Config:
        calls = 0
        async def resources(self, _): self.calls += 1; return {"state": "unconfigured"}
        async def release_idle(self, _): return 'deferred'
    app.state.drawing_repository = Repo()
    app.state.drawing_configuration = Config()
    class DrawingService:
        async def submit_preview(self, service, preset, **values):
            assert values['positive'] == 'cat' and values['mode'] == 'green'
            assert values['expected_version'] == 1 and values['operation_id'] == 'preview-1'
            return uuid4()
    app.state.drawing_service = DrawingService()
    app.include_router(router, prefix="/api/v1")
    return app


def test_get_settings_and_lists_do_not_start_or_preheat_comfy():
    app, service = app_for_tests(), uuid4()
    with TestClient(app) as client:
        assert client.get(f"/api/v1/bots/ai/drawing/{service}/settings").json()["enabled"] is False
        assert client.get(f"/api/v1/bots/ai/drawing/{service}/group-modes").json()["items"][0]["mode"] == "green"
        assert client.get(f"/api/v1/bots/ai/drawing/{service}/presets").json() == {"items": []}
        assert client.get(f"/api/v1/bots/ai/drawing/{service}/tasks").json()["items"] == []
    assert app.state.drawing_configuration.calls == 0


def test_group_workflow_write_validates_scope_fields_and_keeps_legacy_mode_write():
    app, service, preset = app_for_tests(), uuid4(), uuid4()
    calls = []
    def save(*args, **kwargs):
        calls.append((args, kwargs))
        if (kwargs.get('restrict_workflows') is None) != (kwargs.get('allowed_preset_ids') is None):
            raise ValueError('drawing_group_presets_invalid')
        return 1
    app.state.drawing_repository.save_group_mode = save
    path = f'/api/v1/bots/ai/drawing/{service}/group-modes/123'
    headers = {'If-Match': '0', 'Idempotency-Key': 'group-1'}
    with TestClient(app) as client:
        response = client.put(path, headers=headers, json={'mode':'green', 'restrict_workflows':True,
                                                           'allowed_preset_ids':[str(preset)]})
        assert response.status_code == 200 and response.json()['allowed_preset_ids'] == [str(preset)]
        assert calls[-1][1]['restrict_workflows'] is True and calls[-1][1]['allowed_preset_ids'] == [preset]
        assert client.put(path, headers=headers, json={'mode':'r18'}).status_code == 200
        assert client.put(path, headers=headers, json={'mode':'green', 'restrict_workflows':True}).status_code == 422
        assert client.put(path, headers=headers, json={'mode':'green', 'restrict_workflows':'yes',
                                                       'allowed_preset_ids':[]}).status_code == 422


def test_write_requires_version_and_idempotency_headers_and_bounded_json():
    app, service = app_for_tests(), uuid4()
    path = f"/api/v1/bots/ai/drawing/{service}/settings"
    body = {"enabled": True, "keep_drawing_warm": False,
            "default_text_preset_id": None, "default_edit_preset_id": None}
    with TestClient(app) as client:
        assert client.put(path, json=body).status_code == 422
        headers = {"If-Match": "0", "Idempotency-Key": "operation-1"}
        assert client.put(path, json=body, headers=headers).json()["row_version"] == 1
        assert client.put(path, content=b"x" * (320 * 1024 + 1), headers=headers).status_code == 413


def test_prompt_model_pool_has_own_api_and_never_echoes_its_key():
    app, service = app_for_tests(), uuid4()
    captured = []
    app.state.drawing_repository.list_prompt_models = lambda _: {'items': [], 'active_id': None, 'selection_version': 1}

    def save(_service, data, *, profile_id, expected_version, operation_id):
        captured.append((data.name, data.adapter_kind, data.model_id, data.secret_input.get_secret_value(),
                         expected_version, operation_id))
        assert profile_id is None
        return {'id': str(uuid4()), 'name': data.name, 'adapter_kind': data.adapter_kind,
                'model_id': data.model_id, 'base_url': data.base_url, 'network_mode': 'direct',
                'secret_configured': True, 'row_version': 1, 'revision': 1}

    app.state.drawing_repository.save_prompt_model = save
    path = f'/api/v1/bots/ai/drawing/{service}/prompt-models'
    headers = {'If-Match': '0', 'Idempotency-Key': 'prompt-model'}
    body = {'name': '绘图模型', 'adapter_kind': 'openai_chat_completion', 'model_id': 'draw-model',
            'base_url': 'https://example.invalid/v1', 'secret_input': 'synthetic-secret'}
    with TestClient(app) as client:
        assert client.get(path).json() == {'items': [], 'active_id': None, 'selection_version': 1}
        result = client.post(path, json=body, headers=headers)
        assert result.status_code == 201 and result.json()['secret_configured'] is True
        assert 'synthetic-secret' not in result.text
        assert captured == [('绘图模型', 'openai_chat_completion', 'draw-model', 'synthetic-secret', 0, 'prompt-model')]
        assert client.post(path, json={**body, 'adapter_kind': 'al1s_comfyui_vision'}, headers=headers).status_code == 422


def test_platform_proxy_allows_only_expected_prompt_model_methods():
    from al1s.api.meme_proxy import _DRAWING_ROUTES
    matches = [entry for entry in _DRAWING_ROUTES if entry[0].fullmatch('prompt-models')]
    assert len(matches) == 1 and matches[0][1] == {'GET', 'POST'} and matches[0][2] <= 8192
    identity = str(uuid4())
    assert any(entry[0].fullmatch(f'prompt-models/{identity}') and 'DELETE' in entry[1] for entry in _DRAWING_ROUTES)
    assert any(entry[0].fullmatch(f'prompt-models/{identity}/test') and 'POST' in entry[1] for entry in _DRAWING_ROUTES)
    assert any(entry[0].fullmatch('tasks/batch-delete') and 'POST' in entry[1] for entry in _DRAWING_ROUTES)


def test_online_preset_api_validates_model_and_does_not_echo_key():
    from al1s.api.meme_proxy import _DRAWING_ROUTES
    app, service = app_for_tests(), uuid4()
    calls = []
    def save(identity, data, **kwargs):
        calls.append((identity, data.model_id, data.secret_input.get_secret_value(), kwargs))
        return {'id': str(uuid4()), 'number': 6, 'revision': 1, 'row_version': 1}
    app.state.drawing_repository.save_online_preset = save
    path = f'/api/v1/bots/ai/drawing/{service}/online-presets'
    headers = {'If-Match': '0', 'Idempotency-Key': 'online-preset'}
    body = {'name': 'Flare', 'model_id': 'gpt-image-2.6-flare', 'secret_input': 'example-test-key', 'enabled': True}
    with TestClient(app) as client:
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 200 and 'example-test-key' not in response.text
        assert calls[0][1:3] == ('gpt-image-2.6-flare', 'example-test-key')
        assert client.post(path, headers=headers, json={**body, 'model_id': 'unapproved-model'}).status_code == 422
        assert client.post(path, headers=headers, json={**body, 'base_url': 'https://user:pass@example.test/v1'}).status_code == 422
    assert any(item[0].fullmatch('online-presets') and item[1] == {'POST'} for item in _DRAWING_ROUTES)


def test_prompt_model_activation_delete_and_explicit_test_use_the_selected_model():
    app, service, profile = app_for_tests(), uuid4(), uuid4()
    calls = []
    app.state.drawing_repository.activate_prompt_model = lambda *args, **kwargs: calls.append(('activate', args, kwargs)) or {'active_id': str(profile), 'selection_version': 2}
    app.state.drawing_repository.delete_prompt_model = lambda *args, **kwargs: calls.append(('delete', args, kwargs)) or {'deleted': True}
    class Manager:
        async def test_model(self, requested_service, requested_profile):
            calls.append(('test', requested_service, requested_profile))
            return {'state': 'success', 'message': '连接测试通过。'}
    app.state.drawing_service.manager = Manager()
    root = f'/api/v1/bots/ai/drawing/{service}/prompt-models'
    headers = {'If-Match': '1', 'Idempotency-Key': 'operation'}
    with TestClient(app) as client:
        assert client.put(root + '/active', headers=headers, json={'profile_id': str(profile)}).status_code == 200
        assert client.post(root + f'/{profile}/test', json={}).json()['state'] == 'success'
        assert client.delete(root + f'/{profile}', headers=headers).status_code == 200
    assert [item[0] for item in calls] == ['activate', 'test', 'delete']
    assert calls[0][1] == (service, profile) and calls[1][1:] == (service, profile)


def test_task_views_include_owned_progress_and_batch_delete_uses_requested_ids():
    app, service, task = app_for_tests(), uuid4(), uuid4()
    calls = []
    app.state.drawing_repository.list_tasks = lambda requested, **kwargs: (
        calls.append((requested, kwargs)) or {'items': [{'id': str(task), 'processing_stage': 'executing'}], 'next_after_id': None})
    app.state.drawing_repository.get_task = lambda requested, identity: {'id': str(identity), 'positive': 'cat'}
    app.state.drawing_repository.begin_delete_tasks = lambda requested, identities, **kwargs: (
        calls.append((requested, identities, kwargs)) or identities)
    class Queue:
        def position(self, identity):
            assert identity == str(task)
            return 0
    class Runner:
        gpu_queue = Queue()
        def progress(self, identity):
            assert identity == str(task)
            return {'node_id': '7', 'node_name': 'KSampler', 'value': 3, 'max': 20}
        async def delete_history(self, identities):
            return {'deleted': [str(identity) for identity in identities], 'pending': []}
    app.state.drawing_runner = Runner()
    root = f'/api/v1/bots/ai/drawing/{service}/tasks'
    with TestClient(app) as client:
        listing = client.get(root, params={'view': 'active'}).json()
        detail = client.get(root + f'/{task}').json()
        assert listing['items'][0]['queue_position'] == detail['queue_position'] == 0
        assert listing['items'][0]['progress']['node_name'] == 'KSampler'
        assert detail['progress']['value'] == 3
        result = client.post(root + '/batch-delete', json={'task_ids': [str(task)]},
                             headers={'Idempotency-Key': 'delete-history'})
        assert result.status_code == 200 and result.json() == {'deleted': [str(task)], 'pending': []}
    assert calls[0][1]['view'] == 'active'
    assert calls[1] == (service, [task], {'operation_id': 'delete-history'})


def test_qq_runtime_drawing_api_requires_private_control_token():
    from al1s.meme_workshop.runtime import create_runtime_app
    async def run():
        fixture = app_for_tests()
        token, service = "private-runtime-token-" + "x" * 32, uuid4()
        app = create_runtime_app(object(), object(), token, role="qq", ai_repository=object(),
            drawing_repository=fixture.state.drawing_repository,
            drawing_configuration=fixture.state.drawing_configuration)
        path = f"/api/v1/bots/ai/drawing/{service}/settings"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot") as client:
            assert (await client.get(path)).status_code == 401
            result = await client.get(path, headers={"X-AL1S-Meme-Control": token})
            assert result.status_code == 200 and result.json()["enabled"] is False
    asyncio.run(run())


def test_admin_preview_is_explicit_bounded_and_never_needs_qq_identity():
    app, service, preset = app_for_tests(), uuid4(), uuid4()
    path = f"/api/v1/bots/ai/drawing/{service}/presets/{preset}/preview"
    with TestClient(app) as client:
        response = client.post(path, data={"positive": "cat", "negative": "", "mode": "green"},
            files={"image": ("input.png", b"picture", "image/png")},
            headers={"If-Match": "1", "Idempotency-Key": "preview-1"})
        assert response.status_code == 202 and response.json()['task_id']


def test_resource_actions_are_explicit_and_warmup_needs_existing_vision_controller():
    app, service = app_for_tests(), uuid4()
    with TestClient(app) as client:
        base = f'/api/v1/bots/ai/drawing/{service}/resources'
        assert client.post(base + '/release', json={}).json() == {'status': 'deferred'}
        assert client.post(base + '/warmup', json={}).status_code == 503
