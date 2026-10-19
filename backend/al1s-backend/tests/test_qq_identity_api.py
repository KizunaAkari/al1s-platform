from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4
import pytest
from http.client import IncompleteRead

from fastapi import FastAPI
from fastapi.testclient import TestClient

from al1s.api import qq_device_identity
from al1s.app.admin_auth import install_admin_auth
from al1s.app.config import Settings
from al1s.bots.control_proxy import create_proxy
from al1s.bots.qq_identity.control import QqIdentityGateway, identity_response
from al1s.bots.qq_identity.contracts import QqIdentityError
from al1s.bots.qq_identity.repository import QqIdentityRepository
from al1s.components.host.bot_maintenance import BotMaintenanceGuard
from al1s.components.host.journal import HostJournal
from al1s.meme_workshop.runtime import create_runtime_app
from al1s.meme_workshop.transport import BackendResponse

TOKEN = "private-test-control-token-32-characters"
PATH = "/api/v1/bots/qq/device-identity"


@pytest.fixture(autouse=True)
def native_manifest(monkeypatch):
    monkeypatch.setattr("al1s.bots.qq_identity.runtime_routes.native_version", lambda: "8.3.2")


def runtime(tmp_path, role="qq"):
    (tmp_path / "machine_guid.bin").write_bytes(bytes.fromhex("12" * 16))
    repository = QqIdentityRepository(tmp_path)
    app = create_runtime_app(MagicMock(), MagicMock(), TOKEN, role=role, identity_repository=repository)
    return TestClient(app), repository


def test_runtime_requires_private_token_and_is_qq_only(tmp_path):
    client, repository = runtime(tmp_path)
    assert client.get(PATH).status_code == 401
    response = client.get(PATH, headers={"X-AL1S-Meme-Control": TOKEN})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["current_guid"] == repository.guid().hex()
    other, _ = runtime(tmp_path, "discord")
    assert other.get(PATH, headers={"X-AL1S-Meme-Control": TOKEN}).status_code == 404


def test_ops_gateway_locks_qq_mutation_not_discord(tmp_path):
    client, repository = runtime(tmp_path)

    class Backend:
        def forward(self, method, path, body, headers):
            response = client.request(method, path, content=body,
                                      headers={**headers, "X-AL1S-Meme-Control": TOKEN})
            return BackendResponse(response.status_code, response.content, "application/json")

    guard = BotMaintenanceGuard(tmp_path / "maintenance")
    gateway = QqIdentityGateway(Backend(), guard)
    proxy = TestClient(create_proxy(TOKEN, MagicMock(), identity_gateway=gateway))
    headers = {"Authorization": "Bearer " + TOKEN}
    assert proxy.get("/qq/device-identity").status_code == 401
    state = proxy.get("/qq/device-identity", headers=headers).json()
    headers.update({"If-Match": state["revision"], "Idempotency-Key": str(uuid4())})
    body = {"settings": {"device_name": "My QQ", "guid": "12" * 16}}
    with HostJournal(guard.journal.directory).exclusive("bot:discord"):
        assert proxy.put("/qq/device-identity", json=body, headers=headers).status_code == 200
    headers["If-Match"] = repository.read().revision
    headers["Idempotency-Key"] = str(uuid4())
    with HostJournal(guard.journal.directory).exclusive("bot:qq"):
        response = proxy.put("/qq/device-identity", json=body, headers=headers)
        assert response.status_code == 409 and "maintenance" in response.json()["code"]


def test_public_identity_api_requires_admin_and_csrf(tmp_path, monkeypatch):
    _, repository = runtime(tmp_path)
    client = SimpleNamespace(read=repository.read, save=repository.save)
    monkeypatch.setattr(qq_device_identity, "QqIdentityControlClient", lambda *_args: client)
    settings = Settings(admin_password="private-admin-password", admin_cookie_secure=False)
    app = FastAPI()
    app.state.settings = settings
    app.include_router(qq_device_identity.router, prefix="/api/v1")
    install_admin_auth(app, settings)
    api = TestClient(app)
    assert api.get(PATH).status_code == 401
    assert api.put(PATH, json={}, headers={"X-AL1S-CSRF": "1"}).status_code == 401
    assert api.post("/api/v1/auth/login", json={"password": "private-admin-password"},
                    headers={"X-AL1S-CSRF": "1"}).status_code == 200
    assert api.put(PATH, json={}).status_code == 403
    state = api.get(PATH).json()
    response = api.put(PATH, json={"settings": {"device_name": "Mine", "guid": "12" * 16}},
                       headers={"X-AL1S-CSRF": "1", "If-Match": state["revision"],
                                "Idempotency-Key": str(uuid4())})
    assert response.status_code == 200 and response.json()["pending"]
    assert api.get("/api/v1/bots/discord/device-identity").status_code == 404


def test_malformed_or_truncated_private_response_is_safe_and_never_retried(tmp_path):
    with pytest.raises(QqIdentityError):
        identity_response(500, b"[]")
    backend = MagicMock()
    backend.forward.side_effect = IncompleteRead(b"")
    gateway = QqIdentityGateway(backend, BotMaintenanceGuard(tmp_path / "maintenance"))
    with pytest.raises(QqIdentityError, match="unavailable_or_result_unknown"):
        gateway.read()
    assert backend.forward.call_count == 1


def test_proxy_uses_existing_qq_backend_environment_key(tmp_path, monkeypatch):
    from al1s.bots import control_proxy
    from al1s.meme_workshop.transport import MemeBackendClient

    captured = []

    class RecordingClient(MemeBackendClient):
        def __init__(self, url, token, **kwargs):
            captured.append(url)
            super().__init__(url, token, **kwargs)

    monkeypatch.setenv("AL1S_MEME_QQ_BACKEND_URL", "http://qq-backend:3101")
    monkeypatch.delenv("AL1S_QQ_MEME_BACKEND_URL", raising=False)
    monkeypatch.setenv("AL1S_MEME_BACKEND_TOKEN", TOKEN)
    monkeypatch.setenv("AL1S_BOT_CONTROL_TOKEN", TOKEN)
    monkeypatch.setenv("AL1S_CONTROL_QQ_CONTAINER", "al1s-bot-qq")
    monkeypatch.setenv("AL1S_BOT_MAINTENANCE_DIRECTORY", str(tmp_path / "maintenance"))
    monkeypatch.setattr(control_proxy, "MemeBackendClient", RecordingClient)
    control_proxy.from_environment()
    assert captured == ["http://qq-backend:3101"]
