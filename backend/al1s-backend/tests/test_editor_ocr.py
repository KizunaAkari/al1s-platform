from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from al1s.api import editor_ocr
from al1s.app.admin_auth import install_admin_auth
from al1s.execution.editor_sessions import EditorStatus


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(editor_ocr.router, prefix="/api/v1")
    settings = SimpleNamespace(
        admin_password=SecretStr("editor-test-password"),
        cors_origin_list=[],
        admin_cookie_secure=False,
    )
    app.state.settings = settings
    install_admin_auth(app, settings)
    app.state.editor_sessions = SimpleNamespace(
        detail=lambda _: (
            SimpleNamespace(status=EditorStatus.ACTIVE),
            {"ocr_ws_url": "wss://terminal:8766/scrcpy/token/ocr"},
        )
    )
    monkeypatch.setenv("AL1S_EDITOR_RELAY_ORIGINS", "wss://terminal:8766")
    monkeypatch.setenv("AL1S_EDITOR_RELAY_CA_FILE", "test")
    monkeypatch.setattr(editor_ocr.ssl, "create_default_context", lambda **_: None)
    return TestClient(app)


def login(client):
    client.headers["X-AL1S-CSRF"] = "1"
    assert (
        client.post("/api/v1/auth/login", json={"password": "editor-test-password"}).status_code
        == 200
    )


def test_ocr_requires_login_and_rejects_invalid_and_oversized_upload(client):
    path = f"/api/v1/editor-sessions/{uuid4()}/ocr"
    assert client.post(path, headers={"X-AL1S-CSRF": "1"}).status_code == 401
    login(client)
    assert client.post(path, content=b"bad").status_code == 422
    assert client.post(path, content=b"x" * (editor_ocr.MAX_BYTES + 1)).status_code == 413


def test_ocr_forwards_exact_crop_and_returns_text_without_saving(client, monkeypatch):
    body = b"\x89PNG\r\n\x1a\n" + b"x" * 30

    class Upstream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, value):
            assert value == body

        async def recv(self):
            return b'{"texts":["HELLO","123"]}'

    monkeypatch.setattr(editor_ocr, "DirectConnect", lambda *a, **k: Upstream())
    login(client)
    response = client.post(f"/api/v1/editor-sessions/{uuid4()}/ocr", content=body)
    assert response.status_code == 200
    assert response.json() == {"texts": ["HELLO", "123"]}
