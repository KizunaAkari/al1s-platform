import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def load_quiesce():
    script = Path(__file__).resolve().parents[3] / 'al1s-deployment/scripts/qq-unified-quiesce.py'
    spec = importlib.util.spec_from_file_location('qq_unified_quiesce', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configuration():
    return {'enabled': True, 'event_ws_url': 'ws://llbot:3001', 'allow_private': False,
        'groups': ['100'], 'wake_words': [], 'persona': 'fixture', 'vision_enabled': True,
        'stt_enabled': False, 'tts_mode': 'off', 'reply_all_groups': [], 'smart_reply_groups': ['100'],
        'smart_reply': {'style': 'context'}, 'providers': {'chat': {'adapter_kind': 'openai_chat_completion',
            'model_id': 'fixture', 'base_url': '', 'voice': '', 'network_mode': 'proxy',
            'secret_configured': True}}, 'desired_version': 3, 'applied_version': 3, 'last_error': None}


def test_writable_settings_preserve_existing_provider_secret_and_network_options():
    module = load_quiesce()
    payload = module.writable(configuration(), False)
    assert not payload['enabled'] and payload['smart_reply_groups'] == ['100']
    assert 'secret_input' not in payload['providers']['chat']
    assert 'network_mode' not in payload['providers']['chat']
    from al1s.bot_ai.settings import AiSettingsWrite
    assert AiSettingsWrite.model_validate(payload).providers['chat'].network_mode is None


def test_settings_worker_uses_authenticated_local_api_and_version_headers(monkeypatch):
    import io

    script = Path(__file__).resolve().parents[3] / 'al1s-deployment/scripts/qq-unified-settings-worker.py'
    spec = importlib.util.spec_from_file_location('qq_unified_settings_worker', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = {}
    class Opener:
        def open(self, request, timeout):
            captured.update(url=request.full_url, method=request.method, headers=dict(request.header_items()),
                            body=request.data, timeout=timeout)
            return io.BytesIO(b'{"desired_version":4}')
    monkeypatch.setattr(module, 'build_opener', lambda *_: Opener())
    monkeypatch.setenv('AL1S_MEME_BACKEND_TOKEN', 'fixture-token')
    identity = str(uuid4())
    assert module.call('PUT', identity, b'{}', version=3, key='fixture-key') == {'desired_version': 4}
    assert captured['url'] == 'http://127.0.0.1:3101/api/v1/bots/ai/settings/' + identity
    assert captured['method'] == 'PUT' and captured['body'] == b'{}'
    assert captured['headers']['X-al1s-meme-control'] == 'fixture-token'
    assert captured['headers']['If-match'] == '3' and captured['headers']['Idempotency-key'] == 'fixture-key'


def test_pause_and_resume_restore_exact_configuration_with_version_checks(tmp_path, monkeypatch, capsys):
    module = load_quiesce()
    service, state = str(uuid4()), configuration()
    initial = json.loads(json.dumps(state))
    config = tmp_path / 'config.json'
    config.write_text('{}')
    container, image = 'a' * 64, 'sha256:' + 'b' * 64
    row = {'Id': container, 'Image': image, 'State': {'Running': True}, 'Config': {'Labels': {
        'com.docker.compose.project': 'al1s-bot', 'com.docker.compose.service': 'llbot'}}}
    def save(path, value):
        path.write_text(json.dumps(value), encoding='utf-8')
    helper = SimpleNamespace(inspect=lambda _name: row, receipt=save)
    class Journal:
        def __init__(self, _path): pass
        def exclusive(self, _scope): return nullcontext()
        def assert_available(self, *_): pass
    clock = iter(range(0, 1000, 20))
    monkeypatch.setattr(module, 'helpers', lambda: helper)
    monkeypatch.setattr(module, 'HostSettings', SimpleNamespace(model_validate_json=lambda _: SimpleNamespace(state_dir=tmp_path)))
    monkeypatch.setattr(module, 'HostJournal', Journal)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None))
    calls = []
    def worker(action, *, service=None, version=None, key=None, body=None):
        if action == 'inventory':
            return {'enabled': [service_id] if state['enabled'] else [], 'pending': 0, 'active': 0, 'staging': 0}
        if action == 'read':
            return json.loads(json.dumps(state))
        assert version == state['desired_version'] and key
        calls.append(body['enabled'])
        for field, value in body.items():
            if field != 'providers': state[field] = value
        state['desired_version'] += 1
        state['applied_version'] += 1
        return {'enabled': state['enabled'], 'desired_version': state['desired_version'],
                'applied_version': state['applied_version']}
    service_id = service
    monkeypatch.setattr(module, 'worker', worker)
    common = ['--config', str(config), '--expected-qq-container-id', container,
              '--expected-qq-image-id', image, '--execute']
    monkeypatch.setattr('sys.argv', ['qq-unified-quiesce.py', 'pause', *common])
    module.main()
    result = json.loads(capsys.readouterr().out)
    assert result['state'] == 'paused' and state['enabled'] is False
    monkeypatch.setattr('sys.argv', ['qq-unified-quiesce.py', 'resume', *common, '--state-id', result['state_id']])
    state['persona'] = 'other edit'
    state['desired_version'] += 1
    state['applied_version'] += 1
    with pytest.raises(ValueError, match='settings_changed'):
        module.main()
    assert calls == [False] and state['persona'] == 'other edit'
    state['persona'] = initial['persona']
    state['desired_version'] -= 1
    state['applied_version'] -= 1
    module.main()
    assert json.loads(capsys.readouterr().out)['state'] == 'resumed'
    assert calls == [False, True] and state['enabled'] is True
    assert state['providers'] == initial['providers'] and state['persona'] == initial['persona']
    assert state['desired_version'] == initial['desired_version'] + 2


def test_quiesce_rejects_a_changed_or_stopped_container():
    module = load_quiesce()
    helper = SimpleNamespace(inspect=lambda _: {'Id': 'a', 'Image': 'image', 'State': {'Running': False},
        'Config': {'Labels': {'com.docker.compose.project': 'al1s-bot', 'com.docker.compose.service': 'llbot'}}})
    with pytest.raises(ValueError, match='writer_changed'):
        module.running_qq(helper, 'a', 'image')


def test_wait_applied_allows_accepted_sticker_queue_to_finish(monkeypatch):
    module = load_quiesce()
    states = iter([
        {'enabled': False, 'desired_version': 2, 'applied_version': 1, 'last_error': 'ai_sticker_queue_pending'},
        {'enabled': False, 'desired_version': 2, 'applied_version': 2, 'last_error': None},
    ])
    monkeypatch.setattr(module, 'worker', lambda *_args, **_kwargs: next(states))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: None))
    module.wait_applied(str(uuid4()), 2, False)


def test_drain_requires_a_stable_empty_window_and_closed_admission(monkeypatch):
    module = load_quiesce()
    times = iter([0, 0, 1, 2, 3, 10, 16, 20])
    observations = iter([
        {'enabled': [], 'pending': 0, 'active': 0, 'staging': 1},
        {'enabled': [], 'pending': 0, 'active': 0, 'staging': 0},
        {'enabled': [], 'pending': 0, 'active': 0, 'staging': 0},
    ])
    monkeypatch.setattr(module, 'running_qq', lambda *_: None)
    monkeypatch.setattr(module, 'worker', lambda *_: next(observations))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: next(times), sleep=lambda _: None))
    module.wait_drained(None, 'container', 'image', closed=True)
    monkeypatch.setattr(module, 'worker', lambda *_: {'enabled': ['one'], 'pending': 0, 'active': 0, 'staging': 0})
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: None))
    with pytest.raises(ValueError, match='settings_changed'):
        module.wait_drained(None, 'container', 'image', closed=True)
