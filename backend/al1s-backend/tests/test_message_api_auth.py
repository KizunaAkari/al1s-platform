from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from al1s.api.bot_containers import router as containers
from al1s.api.notifications import router as notifications
from al1s.api.reviews import router as reviews
from al1s.app.admin_auth import install_admin_auth
from al1s.app.config import Settings


def build_client():
    app = FastAPI()
    settings = Settings(admin_password="message-test-password", admin_cookie_secure=False)
    app.state.settings = settings
    install_admin_auth(app, settings)
    app.include_router(notifications, prefix="/api/v1/notifications")
    app.include_router(reviews, prefix="/api/v1")
    app.include_router(containers, prefix="/api/v1")
    app.state.notification_commands = MagicMock()
    app.state.reviews = MagicMock()

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.request_id = str(uuid4())
        return await call_next(request)

    return TestClient(app), app


def test_notification_submission_requires_login_and_never_accepts_forward_bypass():
    client, app = build_client()
    body = {
        "source_event_id": str(uuid4()),
        "source_type": "test-module",
        "source_id": str(uuid4()),
        "notification_kind": "storage_low",
        "occurred_at": datetime.now(UTC).isoformat(),
        "summary": "test",
    }
    headers = {"X-AL1S-CSRF": "1"}
    assert (
        client.post("/api/v1/notifications/intents", json=body, headers=headers).status_code == 401
    )
    app.state.notification_commands.materialize_intent.assert_not_called()
    assert (
        client.post(
            "/api/v1/auth/login", json={"password": "message-test-password"}, headers=headers
        ).status_code
        == 200
    )
    app.state.notification_commands.materialize_intent.return_value = SimpleNamespace(
        intent_id=uuid4(),
        source_event_id=uuid4(),
        disposition="no_route",
    )
    result = client.post("/api/v1/notifications/intents", json=body, headers=headers)
    assert result.status_code == 202
    assert result.json()["disposition"] == "no_route"
    assert result.headers["cache-control"] == "no-store"
    body["notification_kind"] = "forward"
    assert (
        client.post("/api/v1/notifications/intents", json=body, headers=headers).status_code == 422
    )


def test_review_body_is_not_read_without_admin_session():
    client, app = build_client()
    result = client.get(f"/api/v1/notifications/reviews/{uuid4()}")
    assert result.status_code == 401
    app.state.reviews.detail.assert_not_called()
    headers = {"X-AL1S-CSRF": "1"}
    client.post("/api/v1/auth/login", json={"password": "message-test-password"}, headers=headers)
    app.state.reviews.detail.return_value = {
        "body_available": True,
        "document": {"text": "private"},
    }
    response = client.get(f"/api/v1/notifications/reviews/{uuid4()}")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    app.state.reviews.detail.assert_called_once()


def test_container_operations_require_login_and_csrf():
    client, _app = build_client()
    body = {"action": "restart", "revision": "0" * 64}
    assert client.get("/api/v1/bots/containers/qq").status_code == 401
    headers = {"X-AL1S-CSRF": "1"}
    assert client.post("/api/v1/bots/containers/qq", json=body,
                       headers=headers).status_code == 401
    client.post("/api/v1/auth/login", json={"password": "message-test-password"}, headers=headers)
    assert client.post("/api/v1/bots/containers/qq", json=body).status_code == 403
    result = client.post("/api/v1/bots/containers/qq", json=body, headers=headers)
    assert result.status_code == 503
    assert result.json()["code"] == "bot_control_not_configured"
