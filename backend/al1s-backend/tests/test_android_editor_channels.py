from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from al1s.api import android_editor_channels as uplink
from al1s.api import editor_channels as browser
from al1s.app.admin_auth import COOKIE, AdminSessions
from al1s.execution.android_editor_transport import AndroidEditorHub
from al1s.execution.editor_sessions import EditorSession, EditorStatus


def fixture():
    now = datetime.now(UTC)
    session = EditorSession(uuid4(), uuid4(), uuid4(), uuid4(), EditorStatus.ACTIVE,
                            now, now + timedelta(seconds=30), now,
                            terminal_instance_id=uuid4())
    app = FastAPI()
    app.state.services = SimpleNamespace()
    app.include_router(uplink.router)
    app.include_router(browser.router)
    app.state.services.android_editor_hub = AndroidEditorHub()
    connection = {"transport": "android-reverse-v1"}
    app.state.services.editor_sessions = SimpleNamespace(detail=lambda _: (session, connection))
    app.state.services.execution_resources = SimpleNamespace(
        authenticate_terminal=lambda _: session
    )
    app.state.settings = SimpleNamespace(cors_origin_list=["https://platform"])
    app.state.services.settings = app.state.settings
    app.state.admin_sessions = AdminSessions("test-password-only")
    _, token = app.state.admin_sessions.login("test-password-only", "test")
    return app, session, {"origin": "https://platform", "cookie": f"{COOKIE}={token}"}


def test_phone_reverse_stream_and_control_are_session_bound():
    app, session, headers = fixture()
    # The authentication repository returns a terminal entity's id.
    app.state.services.execution_resources.authenticate_terminal = lambda _: SimpleNamespace(
        terminal_id=session.terminal_id,
    )
    path = f"/terminal/editor-sessions/{session.session_id}/uplink/control"
    path += f"?instance_id={session.terminal_instance_id}"
    with (TestClient(app) as client,
          client.websocket_connect(path, headers={"authorization": "Bearer test-only"}) as phone,
          client.websocket_connect(
              f"/editor-sessions/{session.session_id}/channels/control", headers=headers,
          ) as operator):
        assert phone.receive_bytes() == b"open"
        operator.send_bytes(b"x" * 14)
        assert phone.receive_bytes() == b"x" * 14
        phone.send_bytes(b"accepted")
        assert operator.receive_bytes() == b"accepted"
    assert not app.state.services.android_editor_hub._channels


def test_stale_instance_never_opens_channel():
    app, session, _ = fixture()
    app.state.services.execution_resources.authenticate_terminal = lambda _: SimpleNamespace(
        terminal_id=session.terminal_id,
    )
    with (TestClient(app) as client, pytest.raises(WebSocketDisconnect),
          client.websocket_connect(
            f"/terminal/editor-sessions/{session.session_id}/uplink/video?instance_id={uuid4()}",
            headers={"authorization": "Bearer test-only"},
          )):
        pass
