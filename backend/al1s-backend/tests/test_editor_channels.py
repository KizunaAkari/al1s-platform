import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from al1s.api import editor_channels as relay
from al1s.app.admin_auth import COOKIE, AdminSessions
from al1s.execution.editor_service import EditorSessionService
from al1s.execution.editor_sessions import EditorSession, EditorStatus


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('AL1S_EDITOR_RELAY_ORIGINS', 'wss://terminal:8766')
    monkeypatch.setenv('AL1S_EDITOR_RELAY_CA_FILE', 'test-ca')
    connection = {f'{kind}_ws_url': f'wss://terminal:8766/scrcpy/token/{kind}'
                  for kind in relay.CHANNELS}
    connection['session_token'] = 'token'
    app = FastAPI()
    app.include_router(relay.router)
    app.state.settings = SimpleNamespace(cors_origin_list=['https://platform'])
    app.state.admin_sessions = AdminSessions('test-password-only')
    _, token = app.state.admin_sessions.login('test-password-only', 'test')
    app.state.editor_sessions = SimpleNamespace(detail=lambda _: (None, connection))
    headers = {'origin': 'https://platform', 'cookie': f'{COOKIE}={token}'}
    path = f'/editor-sessions/{uuid4()}/channels/video'
    return app, connection, headers, path


def test_browser_only_receives_same_origin_paths(setup):
    _, connection, _, _ = setup
    result = relay.browser_connection(uuid4(), connection)
    assert 'session_token' not in result
    assert all(result[f'{kind}_ws_url'].startswith('/api/v1/editor-sessions/')
               for kind in relay.CHANNELS)
    assert connection['session_token'] == 'token'


@pytest.mark.parametrize('url', [
    'ws://terminal:8766/a', 'wss://other:8766/a',
    'wss://user@terminal:8766/a', 'wss://terminal:8766/a?redirect=other',
])
def test_target_allowlist(setup, url):
    assert not relay.allowed_target(url)


@pytest.mark.parametrize('change', ['cookie', 'origin', 'authorization', 'channel', 'target'])
def test_rejects_before_opening_upstream(setup, monkeypatch, change):
    app, connection, headers, path = setup
    if change in {'cookie', 'origin'}:
        headers.pop(change)
    elif change == 'authorization':
        headers['authorization'] = 'Bearer terminal-token'
    elif change == 'channel':
        path = path.replace('/video', '/shell')
    else:
        connection['video_ws_url'] = 'wss://untrusted/video'
    def forbidden(*args, **kwargs):
        pytest.fail('unauthorized upstream access')
    monkeypatch.setattr(relay, 'DirectConnect', forbidden)
    with TestClient(app) as client:
        with (pytest.raises(WebSocketDisconnect) as error,
              client.websocket_connect(path, headers=headers)):
            pass
        assert error.value.code == 1008


@pytest.mark.parametrize('channel', ['video', 'control', 'screenshot'])
def test_binary_backpressure_and_disconnect_cleanup(setup, monkeypatch, channel):
    app, _, headers, path = setup
    state = {'closed': False, 'sent': []}
    class Upstream:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            state['closed'] = True
        async def send(self, packet):
            state['sent'].append(packet)
        async def __aiter__(self):
            yield b'frame'
            await asyncio.Event().wait()
    def connect(target, **kwargs):
        assert kwargs['proxy'] is None
        assert kwargs['ssl'] == 'verified-context'
        assert kwargs['max_queue'] == 2
        return Upstream()
    monkeypatch.setattr(relay.ssl, 'create_default_context', lambda **kw: 'verified-context')
    monkeypatch.setattr(relay, 'DirectConnect', connect)
    with (TestClient(app) as client,
          client.websocket_connect(path.replace('/video', '/'+channel), headers=headers) as ws):
        assert ws.receive_bytes() == b'frame'
        ws.send_bytes(b'x' * 14)
        if channel != 'control':
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_bytes()
            assert error.value.code == 1008
    assert state['closed']
    assert state['sent'] == ([b'x' * 14] if channel == 'control' else [])


def test_redirects_are_not_followed():
    error = RuntimeError('redirect')
    assert relay.DirectConnect.process_redirect(None, error) is error


@pytest.mark.parametrize('status', [
    EditorStatus.ACTIVE, EditorStatus.CLOSING, EditorStatus.PENDING,
])
def test_current_lease_does_not_force_release_active_or_closing(status):
    now = datetime.now(UTC)
    row = EditorSession(uuid4(), uuid4(), uuid4(), uuid4(), status,
                        now - timedelta(minutes=1), now - timedelta(seconds=1), now)
    writes = []
    tx = SimpleNamespace(target_terminal=lambda _: row.terminal_id,
                         by_device=lambda _: row, put=lambda new, old: writes.append(new))
    @contextmanager
    def transactions():
        yield tx
    service = EditorSessionService(transactions, None, clock=lambda: now)
    current = service.current(row.device_id)
    if status == EditorStatus.PENDING:
        assert current is None
        assert writes[0].status == EditorStatus.EXPIRED
    else:
        assert current == row
    tx.target_terminal = lambda _: uuid4()
    assert service.current(row.device_id) is None

def test_slow_browser_backpressure_terminates_pump_without_buffering_forever():
    async def run():
        finalized = []
        class Upstream:
            async def __aiter__(self):
                try:
                    yield b'frame'
                    await asyncio.Event().wait()
                finally:
                    finalized.append(True)
        class Socket:
            async def send_bytes(self, _body):
                await asyncio.Event().wait()
            async def receive(self):
                await asyncio.Event().wait()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(relay.pump(Socket(), Upstream(), 'video'), 4)
        assert finalized
    asyncio.run(run())
