from fastapi import FastAPI
from fastapi.testclient import TestClient

from al1s.app.admin_auth import AdminSessions, device_route, install_admin_auth
from al1s.app.config import Settings


def client():
    app = FastAPI()
    settings = Settings(admin_password="test-admin-password", admin_cookie_secure=False)
    app.state.settings = settings
    install_admin_auth(app, settings)
    @app.get("/api/v1/private")
    def private():
        return {"ok": True}
    return TestClient(app)


def test_login_cookie_logout_and_csrf():
    c = client()
    assert c.get("/api/v1/private").status_code == 401
    assert c.post("/api/v1/auth/login", json={"password": "test-admin-password"}).status_code == 403
    headers = {"X-AL1S-CSRF": "1"}
    response = c.post("/api/v1/auth/login", headers=headers,
                      json={"password": "test-admin-password"})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert c.get("/api/v1/private").status_code == 200
    foreign = {**headers, "Origin": "https://evil.example"}
    assert c.post("/api/v1/auth/logout", headers=foreign).status_code == 403
    assert c.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert c.get("/api/v1/private").status_code == 401


def test_rate_limit_and_unconfigured_fail_closed():
    sessions = AdminSessions("test-admin-password")
    for _ in range(5):
        assert sessions.login("wrong", "peer")[0] == 401
    assert sessions.login("test-admin-password", "peer")[0] == 429
    assert AdminSessions("").login("", "peer")[0] == 503


def test_device_exemption_never_covers_management():
    assert not device_route("/api/v1/target-devices/adb-discoveries")
    device = "11111111-1111-4111-8111-111111111111"
    assert not device_route(f"/api/v1/target-devices/{device}/display-name")
    assert not device_route(f"/api/v1/target-devices/discoveries/{device}/connect")
    c = client()
    assert c.get("/api/v1/target-devices/adb-discoveries").status_code == 401
    assert c.patch(
        f"/api/v1/target-devices/{device}/display-name",
        headers={"X-AL1S-CSRF": "1"},
        json={"expected_version": 1, "display_name": "Renamed"},
    ).status_code == 401
    assert c.post(
        f"/api/v1/target-devices/discoveries/{device}/connect",
        headers={"X-AL1S-CSRF": "1"},
    ).status_code == 401
    assert device_route("/api/v1/bots/worker/config")
    assert device_route("/api/v1/terminals/register")
    assert not device_route("/api/v1/bots/services")
    assert not device_route("/api/v1/terminals/registration-grants")
    assert not device_route("/api/v1/notifications/channels")
    script = "11111111-1111-4111-8111-111111111111"
    assert device_route(f"/api/v1/maa/scripts/{script}/quick-test-results")
    assert not device_route(f"/api/v1/maa/scripts/{script}/quick-test-definition")
    assert not device_route(f"/api/v1/maa/scripts/{script}/quick-tests/{script}")
