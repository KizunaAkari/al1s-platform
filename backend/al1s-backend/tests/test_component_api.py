from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from al1s.api.components import router
from al1s.app.admin_auth import install_admin_auth
from al1s.app.config import Settings
from al1s.components.contracts import ComponentRelease, InventoryPage
from al1s.execution.host_management import HostManagerConnection
from tests.test_component_inventory import release


def test_inventory_is_admin_only_and_internal_downloads_require_independent_host_identity():
    app = FastAPI()
    settings = Settings(
        admin_password="private-admin-password",
        admin_cookie_secure=False,
        component_host=HostManagerConnection(url="https://host.local", token="x" * 32),
    )
    app.state.settings = settings
    app.state.component_inventory = SimpleNamespace(list=lambda *_: InventoryPage(items=[]))
    record = ComponentRelease(
        **release().model_dump(),
        state="published",
        row_version=1,
        blob_id=uuid4(),
        created_at=datetime.now(UTC),
    )
    app.state.component_releases = SimpleNamespace(
        store=SimpleNamespace(release_object_key=lambda *_: [record])
    )
    app.include_router(router, prefix="/api/v1")
    install_admin_auth(app, settings)
    from fastapi.responses import JSONResponse

    from al1s.execution.errors import ExecutionDomainError

    @app.exception_handler(ExecutionDomainError)
    def error(_, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)

    with TestClient(app) as api:
        assert api.get("/api/v1/components/inventory").status_code == 401
        path = f"/api/v1/components/internal/releases/{uuid4()}"
        assert api.get(path).status_code == 401
        assert api.get(path, headers={"Authorization": "Bearer " + "x" * 32}).status_code == 200
        assert (
            api.post(
                "/api/v1/auth/login",
                json={"password": "private-admin-password"},
                headers={"X-AL1S-CSRF": "1"},
            ).status_code
            == 200
        )
        assert api.get("/api/v1/components/inventory").status_code == 200
