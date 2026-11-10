import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from al1s.bots.control_proxy import create_proxy
from al1s.bots.log_reader import BotLogReader
from al1s.bots.retained_logs import query_logs
from tests.test_message_api_auth import build_client


def append_log(directory, when, container, message, priority=30):
    stamp = when.isoformat().replace('+00:00', 'Z')
    payload = f'<{priority}>1 {stamp} host al1s/{container} 119 al1s/{container} - {message}'
    record = {'occurred_at': when.isoformat(), 'syslog_b64': base64.b64encode(payload.encode()).decode()}
    with (directory / f'{when:%Y%m%d%H}.jsonl').open('a', encoding='utf-8') as output:
        output.write(json.dumps(record) + '\n')


def test_log_sources_and_retention_follow_the_selected_bot(tmp_path):
    now = datetime(2026, 11, 4, 10, 30, tzinfo=UTC)
    append_log(tmp_path, now - timedelta(days=8), 'al1s-bot-qq', 'old')
    append_log(tmp_path, now - timedelta(minutes=4), 'al1s-bot-qq', 'qq event')
    append_log(tmp_path, now - timedelta(minutes=3), 'al1s-bot-discord', 'discord event')
    append_log(tmp_path, now - timedelta(minutes=2), 'al1s-platform-app', 'backend event')
    append_log(tmp_path, now - timedelta(minutes=1), 'al1s-platform-gateway', 'gateway event')
    append_log(tmp_path, now, 'unexpected-container', 'not allowed')
    result = query_logs(tmp_path, 'qq', 'all', 'all', now=now)
    assert [row['source'] for row in result['items']] == ['gateway', 'backend', 'bot']
    assert [row['message'] for row in result['items']] == ['gateway event', 'backend event', 'qq event']
    assert query_logs(tmp_path, 'discord', 'bot', 'all', now=now)['items'][0]['message'] == 'discord event'
    with pytest.raises(ValueError):
        query_logs(tmp_path, 'qq', '../../secrets', 'all', now=now)


def test_important_filter_keeps_errors_and_connection_events_and_masks_credentials(tmp_path):
    now = datetime(2026, 11, 4, 10, 30, tzinfo=UTC)
    append_log(tmp_path, now - timedelta(seconds=3), 'al1s-bot-qq', 'ordinary info')
    append_log(tmp_path, now - timedelta(seconds=2), 'al1s-bot-qq', 'WebSocket disconnected')
    append_log(tmp_path, now - timedelta(seconds=1), 'al1s-bot-qq',
               '{"level":"error","event":"request failed","token":"secret-one",'
               '"authorization":"Bearer secret-two","detail":"https://example.test/x?api_key=secret-three"}')
    items = query_logs(tmp_path, 'qq', 'bot', 'important', now=now)['items']
    assert len(items) == 2 and items[0]['level'] == 'error'
    assert 'disconnected' in items[1]['message']
    body = json.dumps(items, ensure_ascii=False)
    assert all(secret not in body for secret in ('secret-one', 'secret-two', 'secret-three'))


def test_reader_handles_corrupt_records_and_bounds_scanning(tmp_path):
    now = datetime(2026, 11, 4, 10, 30, tzinfo=UTC)
    append_log(tmp_path, now - timedelta(seconds=1), 'al1s-platform-app', 'x' * 2000)
    append_log(tmp_path, now, 'al1s-platform-app', 'ERROR last event', priority=27)
    with (tmp_path / f'{now:%Y%m%d%H}.jsonl').open('a', encoding='utf-8') as output:
        output.write('{bad json}\n')
    result = query_logs(tmp_path, 'qq', 'backend', 'all', now=now, max_bytes=512)
    assert result['scan_limited'] is True
    assert len(result['items']) == 1 and result['items'][0]['message'] == 'ERROR last event'


def test_log_reader_uses_only_the_fixed_collector_and_validated_filters():
    runner = Mock()
    runner.run.return_value = json.dumps({'observed_at': '2026-11-04T10:30:00+00:00', 'items': [], 'scan_limited': False})
    reader = BotLogReader(runner)
    assert reader.read('qq', 'gateway', 'important')['items'] == []
    command = runner.run.call_args.args[0]
    assert command[:5] == ['docker', 'exec', 'al1s-platform-log-collector', 'python', '-c']
    assert command[-3:] == ['qq', 'gateway', 'important']
    with pytest.raises(ValueError):
        reader.read('qq', '../../secrets', 'all')
    assert runner.run.call_count == 1


def test_internal_log_endpoint_is_authenticated_and_rejects_unapproved_sources():
    reader = Mock()
    reader.read.return_value = {'observed_at': '2026-11-04T10:30:00+00:00', 'items': [], 'scan_limited': False}
    client = TestClient(create_proxy('x' * 32, Mock(), log_reader=reader))
    assert client.get('/logs/qq').status_code == 401
    headers = {'Authorization': 'Bearer ' + 'x' * 32}
    assert client.get('/logs/qq?source=../../etc', headers=headers).status_code == 422
    assert client.get('/logs/platform', headers=headers).status_code == 422
    response = client.get('/logs/qq?source=backend&level=important', headers=headers)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    reader.read.assert_called_once_with('qq', 'backend', 'important')


def test_public_log_endpoint_requires_admin_and_disables_caching(monkeypatch):
    client, _ = build_client()
    assert client.get('/api/v1/bots/containers/qq/logs').status_code == 401
    import al1s.api.bot_containers as module
    fake = Mock()
    fake.logs.return_value = {'observed_at': '2026-11-04T10:30:00+00:00', 'items': [], 'scan_limited': False}
    monkeypatch.setattr(module, 'ControlClient', lambda *_: fake)
    client.post('/api/v1/auth/login', json={'password': 'message-test-password'}, headers={'X-AL1S-CSRF': '1'})
    response = client.get('/api/v1/bots/containers/qq/logs?source=gateway&level=all')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    fake.logs.assert_called_once_with('qq', 'gateway', 'all')
    assert client.get('/api/v1/bots/containers/qq/logs?source=../../etc').status_code == 422
