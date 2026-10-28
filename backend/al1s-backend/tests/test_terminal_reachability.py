import asyncio
from uuid import uuid4

import pytest

from al1s.api import terminal_reachability as module
from al1s.execution.errors import AuthenticationError


class Socket:
    def __init__(self, headers=None, incoming=None):
        self.headers = headers or {}
        self.incoming = incoming
        self.accepted = False
        self.frames = []
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def send_text(self, data):
        self.frames.append(data)

    async def receive(self):
        if self.incoming:
            return self.incoming
        await asyncio.Future()

    async def close(self, code=1000):
        self.closed = code


def test_browser_origin_is_rejected_before_authentication(monkeypatch):
    socket = Socket({'origin': 'https://platform', 'authorization': 'Bearer valid'})
    monkeypatch.setattr(
        module, 'authenticated_terminal_id',
        lambda *_: pytest.fail('must not authenticate browser'),
    )
    asyncio.run(module.terminal_reachability(socket))
    assert not socket.accepted and socket.closed == 1008


def test_missing_or_revoked_credential_never_opens_socket(monkeypatch):
    def reject(*_):
        raise AuthenticationError()
    monkeypatch.setattr(module, 'authenticated_terminal_id', reject)
    socket = Socket()
    asyncio.run(module.terminal_reachability(socket))
    assert not socket.accepted and socket.closed == 1008


def test_fixed_messages_and_periodic_revocation_cancel_both_loops(monkeypatch):
    calls = []
    terminal = uuid4()
    def authenticate(_, credential):
        calls.append(credential)
        if len(calls) > 1:
            raise AuthenticationError()
        return terminal
    monkeypatch.setattr(module, 'authenticated_terminal_id', authenticate)
    monkeypatch.setattr(module, 'PULSE_SECONDS', .001)
    monkeypatch.setattr(module, 'AUTH_SECONDS', .002)
    socket = Socket({'authorization': 'Bearer test-only'})
    asyncio.run(asyncio.wait_for(module.terminal_reachability(socket), .5))
    assert socket.accepted and socket.closed == 1008
    assert socket.frames and set(socket.frames) == {'al1s-reachability-v1'}
    assert len(calls) == 2


@pytest.mark.parametrize('message', [
    {'type': 'websocket.disconnect'},
    {'type': 'websocket.receive', 'text': 'run something'},
])
def test_disconnect_or_unexpected_payload_does_not_run_business_work(monkeypatch, message):
    monkeypatch.setattr(module, 'authenticated_terminal_id', lambda *_: uuid4())
    socket = Socket({'authorization': 'Bearer test-only'}, incoming=message)
    asyncio.run(asyncio.wait_for(module.terminal_reachability(socket), .5))
    assert socket.accepted and socket.closed in (1000, 1008)
