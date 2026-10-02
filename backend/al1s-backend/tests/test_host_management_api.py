from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from al1s.adapters.postgres.scheduling_repositories import PostgresExecutionRepository
from al1s.api.host_management import router
from al1s.app.admin_auth import install_admin_auth
from al1s.app.config import Settings
from al1s.execution.host_management import HostManagerConnection


def client_and_app():
    app = FastAPI()
    settings = Settings(admin_password="maintenance-test-password", admin_cookie_secure=False)
    app.state.settings = settings
    install_admin_auth(app, settings)
    app.include_router(router, prefix="/api/v1")
    app.state.execution_resources = MagicMock()
    app.state.execution_scheduling = MagicMock()
    return TestClient(app), app


def test_management_reads_and_writes_require_admin_and_csrf():
    client, app = client_and_app()
    base = f"/api/v1/terminals/{uuid4()}/maintenance"
    assert client.get(base + "/impact").status_code == 401
    assert client.get(base + "/logs").status_code == 401
    assert client.post(base + "/recovery", json={"deployment_id": "release-1"},
                       headers={"X-AL1S-CSRF": "1"}).status_code == 401
    assert client.post(base + "/commands", json={},
                       headers={"X-AL1S-CSRF": "1"}).status_code == 401
    app.state.execution_resources.require_linux_terminal.assert_not_called()
    client.post("/api/v1/auth/login", json={"password": "maintenance-test-password"},
                headers={"X-AL1S-CSRF": "1"})
    assert client.post(base + "/commands", json={}).status_code == 403
    assert client.post(base + "/recovery", json={"deployment_id": "release-1"}).status_code == 403
    assert client.post(base + "/recovery", json={"deployment_id": "../escape"},
                       headers={"X-AL1S-CSRF": "1"}).status_code == 422


def test_terminal_logs_are_admin_only_and_proxy_fixed_host_path():
    client, app = client_and_app()
    terminal = uuid4()
    app.state.settings.host_managers = {
        terminal: HostManagerConnection(url="https://host:8771", token="x" * 32)
    }
    client.post("/api/v1/auth/login", json={"password": "maintenance-test-password"},
                headers={"X-AL1S-CSRF": "1"})
    with patch("al1s.api.host_management.HostManagerClient") as host:
        host.return_value.request.return_value = {
            "observed_at": "2026-09-25T00:00:00Z", "lines": ["safe event"]
        }
        response = client.get(f"/api/v1/terminals/{terminal}/maintenance/logs")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["lines"] == ["safe event"]
    app.state.execution_resources.require_linux_terminal.assert_called_once_with(terminal)
    host.return_value.request.assert_called_once_with("/v1/logs")


def test_impact_is_bounded_and_does_not_require_host_credentials():
    client, app = client_and_app()
    terminal = uuid4()
    client.post("/api/v1/auth/login", json={"password": "maintenance-test-password"},
                headers={"X-AL1S-CSRF": "1"})
    app.state.execution_scheduling.maintenance_impact.return_value = [
        {"execution_id": str(uuid4()), "name": f"task-{n}", "status": "running"}
        for n in range(101)
    ]
    result = client.get(f"/api/v1/terminals/{terminal}/maintenance/impact")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert len(result.json()["items"]) == 100
    assert result.json()["truncated"] is True
    app.state.execution_resources.require_linux_terminal.assert_called_once_with(terminal)
    app.state.execution_scheduling.maintenance_impact.assert_called_once_with(terminal)


def test_impact_repository_uses_one_filtered_bounded_join():
    session = MagicMock()
    execution, task, terminal = uuid4(), uuid4(), uuid4()
    session.execute.return_value.all.return_value = [
        SimpleNamespace(id=execution, task_request_id=task, name="task", status="running")
    ]
    result = PostgresExecutionRepository(session).maintenance_impact(terminal)
    assert result[0]["execution_id"] == str(execution)
    session.execute.assert_called_once()
    statement = session.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "JOIN task_requests" in str(compiled)
    assert terminal in compiled.params.values()
    assert 101 in compiled.params.values()
    assert ["waiting", "queued", "running"] in compiled.params.values()
