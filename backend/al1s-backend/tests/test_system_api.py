import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from al1s.app.config import Settings
from al1s.app.factory import create_app
from al1s.infrastructure.readiness import ReadinessResult
from al1s.kernel.storage_gc import StorageGcRequest
from al1s.kernel.types import KernelMaintenanceSnapshot


class FakeReadinessService:
    def __init__(self, dependencies: Mapping[str, dict[str, str]]) -> None:
        self._dependencies = dict(dependencies)

    def check(self) -> ReadinessResult:
        ready = all(item["status"] == "ready" for item in self._dependencies.values())
        return ReadinessResult(ready=ready, dependencies=self._dependencies)

    def close(self) -> None:
        return


class FakeMaintenanceService:
    def snapshot(self) -> KernelMaintenanceSnapshot:
        return KernelMaintenanceSnapshot(
            pending_outbox=7,
            processing_outbox=2,
            dead_letter_outbox=1,
            oldest_pending_outbox_at=None,
            pending_gc_jobs=3,
            processing_gc_jobs=1,
            dead_letter_gc_jobs=4,
            pending_blobs=5,
            quarantined_blobs=6,
            reclaimable_blobs=2,
            reclaimable_bytes=1024,
        )


class FakeStorageGcService:
    def __init__(self) -> None:
        self.submissions = 0
        self.record = StorageGcRequest(
            uuid4(), "pending", datetime.now(UTC), None, 0, 0, 0, 0, 0, None,
        )

    def latest(self) -> StorageGcRequest | None:
        return self.record if self.submissions else None

    def submit(self) -> StorageGcRequest:
        self.submissions += 1
        return self.record


def get(
    app: FastAPI,
    path: str,
    headers: dict[str, str] | None = None,
    *,
    raise_app_exceptions: bool = True,
    authenticate: bool = False,
) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(
            app=app,
            raise_app_exceptions=raise_app_exceptions,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="https://test") as client,
        ):
            if authenticate:
                login = await client.post(
                    "/api/v1/auth/login", headers={"X-AL1S-CSRF": "1"},
                    json={"password": app.state.settings.admin_password.get_secret_value()},
                )
                assert login.status_code == 200
            return await client.get(path, headers=headers)

    return asyncio.run(request())


def test_live_echoes_request_id_without_probing_dependencies() -> None:
    service = FakeReadinessService({"postgresql": {"status": "not_ready"}})
    app = create_app(Settings(), readiness_service=service)  # type: ignore[arg-type]

    response = get(
        app,
        "/api/v1/system/health/live",
        headers={"X-Request-ID": "test-request"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "test-request"
    assert response.json()["status"] == "alive"
    assert response.json()["request_id"] == "test-request"


def test_ready_returns_all_dependency_states() -> None:
    dependencies = {
        "postgresql": {"status": "ready"},
        "s3": {"status": "ready"},
        "mqtt": {"status": "ready"},
    }
    app = create_app(
        Settings(),
        readiness_service=FakeReadinessService(dependencies),  # type: ignore[arg-type]
    )

    response = get(app, "/api/v1/system/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["dependencies"] == dependencies


def test_ready_returns_503_without_leaking_dependency_details() -> None:
    dependencies = {
        "postgresql": {"status": "ready"},
        "s3": {"status": "not_ready", "reason": "EndpointConnectionError"},
        "mqtt": {"status": "ready"},
    }
    app = create_app(
        Settings(),
        readiness_service=FakeReadinessService(dependencies),  # type: ignore[arg-type]
    )

    response = get(app, "/api/v1/system/health/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "not_ready"
    assert body["dependencies"]["s3"] == dependencies["s3"]
    assert "change-me" not in response.text


def test_info_excludes_secrets_and_connection_strings() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://user:private@db/platform",
        s3_secret_key="private-s3",
        admin_password="test-system-password",
    )
    app = create_app(
        settings,
        readiness_service=FakeReadinessService({}),  # type: ignore[arg-type]
    )

    assert get(app, "/api/v1/system/info").status_code == 401
    response = get(app, "/api/v1/system/info", authenticate=True)

    assert response.status_code == 200
    assert response.json()["modules"] == ["platform", "maa", "information", "lineup"]
    assert "private" not in response.text


def test_maintenance_endpoints_return_only_bounded_aggregate_state() -> None:
    app = create_app(
        Settings(admin_password="test-system-password"),
        readiness_service=FakeReadinessService({}),  # type: ignore[arg-type]
        maintenance_service=FakeMaintenanceService(),  # type: ignore[arg-type]
    )

    assert get(app, "/api/v1/system/maintenance/messaging").status_code == 401
    messaging = get(app, "/api/v1/system/maintenance/messaging", authenticate=True)
    storage = get(app, "/api/v1/system/maintenance/storage", authenticate=True)

    assert messaging.status_code == 200
    assert messaging.json() == {
        "pending": 7,
        "processing": 2,
        "dead_letter": 1,
        "oldest_pending_at": None,
    }
    assert storage.status_code == 200
    assert storage.json() == {
        "pending_blobs": 5,
        "quarantined_blobs": 6,
        "pending_gc_jobs": 3,
        "processing_gc_jobs": 1,
        "dead_letter_gc_jobs": 4,
        "reclaimable_blobs": 2,
        "reclaimable_bytes": 1024,
        "automatic_interval_seconds": 30,
    }


def test_storage_cleanup_requires_admin_session_and_csrf() -> None:
    commands = FakeStorageGcService()
    app = create_app(
        Settings(admin_password="test-system-password"),
        readiness_service=FakeReadinessService({}),  # type: ignore[arg-type]
        storage_gc_service=commands,  # type: ignore[arg-type]
    )

    async def request() -> tuple[int, int, int, dict[str, object]]:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="https://test") as client,
        ):
            path = "/api/v1/system/maintenance/storage/cleanup"
            unauthorized = await client.post(path, headers={"X-AL1S-CSRF": "1"})
            login = await client.post(
                "/api/v1/auth/login", headers={"X-AL1S-CSRF": "1"},
                json={"password": "test-system-password"},
            )
            assert login.status_code == 200
            no_csrf = await client.post(path)
            accepted = await client.post(path, headers={"X-AL1S-CSRF": "1"})
            return (
                unauthorized.status_code, no_csrf.status_code,
                accepted.status_code, accepted.json(),
            )

    unauthorized, no_csrf, accepted, body = asyncio.run(request())
    assert (unauthorized, no_csrf, accepted) == (401, 403, 200)
    assert body["status"] == "pending"
    assert commands.submissions == 1


def test_unhandled_error_does_not_leak_exception_message_to_response_or_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_marker = "stage2-secret-marker"
    app = create_app(
        Settings(),
        readiness_service=FakeReadinessService({}),  # type: ignore[arg-type]
    )

    @app.get("/test/unhandled-error")
    def unhandled_error() -> None:
        raise RuntimeError(f"do not expose {secret_marker}")

    response = get(
        app,
        "/test/unhandled-error",
        raise_app_exceptions=False,
    )
    captured = capsys.readouterr()

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert secret_marker not in response.text
    assert secret_marker not in captured.out
    assert secret_marker not in captured.err
