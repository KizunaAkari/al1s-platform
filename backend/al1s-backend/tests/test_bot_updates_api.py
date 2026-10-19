from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from al1s.api.bot_updates import router as updates
from al1s.api.components import router as components
from al1s.app.admin_auth import install_admin_auth
from al1s.app.config import Settings
from al1s.components.contracts import ComponentRelease, OperationPage
from tests.test_bot_update_contracts import registry


def test_bot_updates_require_admin_csrf_and_history_uses_fixed_bot_scope():
    calls = []
    settings = Settings(admin_password="private-admin-password", admin_cookie_secure=False)
    app = FastAPI()
    app.state.settings = settings
    app.state.component_upgrades = SimpleNamespace(
        store=SimpleNamespace(
            list_operations=lambda *args: (calls.append(args), OperationPage(items=[]))[1]
        )
    )
    app.include_router(updates, prefix="/api/v1")
    app.include_router(components, prefix="/api/v1")
    install_admin_auth(app, settings)
    with TestClient(app) as api:
        assert api.get("/api/v1/bots/updates/qq").status_code == 401
        assert api.get("/api/v1/bots/updates/qq/versions").status_code == 401
        assert (
            api.post("/api/v1/bots/updates/qq/check", headers={"X-AL1S-CSRF": "1"}).status_code
            == 401
        )
        assert (
            api.post(
                "/api/v1/auth/login",
                json={"password": "private-admin-password"},
                headers={"X-AL1S-CSRF": "1"},
            ).status_code
            == 200
        )
        assert api.post("/api/v1/bots/updates/qq/check").status_code == 403
        assert api.get("/api/v1/components/operations?bot_alias=qq").status_code == 200
        assert calls == [(25, None, "bot:qq")]
        assert api.get("/api/v1/components/operations?bot_alias=arbitrary").status_code == 422


def test_host_manifest_reads_registry_metadata_without_requiring_an_archive():
    values = registry()
    values.update(state="verifying")
    row = ComponentRelease(**values)
    calls = []
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        component_host=SimpleNamespace(
            token=Settings(admin_password="private-admin-password").admin_password
        )
    )
    app.state.component_releases = SimpleNamespace(
        store=SimpleNamespace(
            host_manifest=lambda identity: (calls.append(identity), row)[1],
        )
    )
    app.include_router(components, prefix="/api/v1")
    with TestClient(app) as api:
        response = api.get(
            f"/api/v1/components/internal/releases/{row.release_id}",
            headers={"Authorization": "Bearer private-admin-password"},
        )
    assert response.status_code == 200
    assert response.json()["blob_id"] is None
    assert calls == [row.release_id]


def test_bot_version_endpoint_defaults_to_five_and_caps_history_pages():
    from al1s.components.contracts import ReleasePage

    calls = []
    app = FastAPI()
    app.state.component_releases = SimpleNamespace(
        list_bot=lambda *args: (calls.append(args), ReleasePage(items=[]))[1]
    )
    app.include_router(updates, prefix="/api/v1")
    with TestClient(app) as api:
        assert api.get("/api/v1/bots/updates/qq/versions").status_code == 200
        assert calls == [("qq", 5, None)]
        assert api.get("/api/v1/bots/updates/discord/versions?limit=26").status_code == 422
        assert api.get("/api/v1/bots/updates/unknown/versions").status_code == 422
