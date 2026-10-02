import hashlib
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.blob_references import no_blob_references
from al1s.adapters.postgres.models import BlobObjectRow
from al1s.adapters.postgres.release_repository import ReleaseRepository
from al1s.api.linux_releases import router
from al1s.app.admin_auth import install_admin_auth
from al1s.app.config import Settings
from al1s.execution.errors import ConflictError, ExecutionDomainError, InvalidRequestError
from al1s.execution.host_management import HostManagerConnection
from al1s.releases.contracts import ReleaseInput
from al1s.releases.service import ReleaseService


def test_published_platform_to_host_download_contract(repository, tmp_path):
    downloader = pytest.importorskip("terminal_deployer.download")
    body = b"immutable-release-contract" * 200000
    objects = MagicMock()
    objects.get_range.side_effect = lambda key, start, end: body[start : end + 1]
    service = ReleaseService(repository, objects)
    release = service.create(
        release_input(
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
    )
    repository.enqueue(release.release_id, release.row_version)
    assert service.verify_once()
    terminal = uuid4()
    app = FastAPI()
    app.state.settings = Settings(
        host_managers={
            terminal: HostManagerConnection(url="https://host.local", token="x" * 32),
        }
    )
    app.state.execution_resources = MagicMock()
    app.state.linux_releases = service
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as api:

        def bridge(request):
            response = api.get(request.url.raw_path.decode(), headers=dict(request.headers))
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=httpx.ByteStream(response.content),
            )

        client = downloader.PlatformReleaseClient(
            "https://platform.local",
            terminal,
            "x" * 32,
            transport=httpx.MockTransport(bridge),
            clock=lambda: 100,
        )
        try:
            manifest = client.metadata(release.release_id, 200)
            output = client.download(manifest, tmp_path / "artifacts", 200)
            assert output.read_bytes() == body
            assert manifest.expected_image_id == release.expected_image_id
        finally:
            client.close()


def release_input(**changes):
    return ReleaseInput.model_validate(
        {
            "release_id": uuid4(),
            "version": str(uuid4()),
            "size_bytes": 123,
            "sha256": "a" * 64,
            "candidate_image": "al1s-terminal-next:test-v1",
            "expected_image_id": "sha256:" + "b" * 64,
            **changes,
        }
    )


@pytest.fixture
def repository():
    url = os.environ.get("AL1S_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated PostgreSQL required")
    engine = create_engine(url)
    with engine.connect() as connection:
        transaction = connection.begin()
        sessions = sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield ReleaseRepository(sessions)
        finally:
            transaction.rollback()
    engine.dispose()


def test_create_identity_and_version_are_immutable(repository):
    body, now = release_input(), datetime.now(UTC)
    first = repository.create(body, now)
    assert repository.create(body, now) == first
    with pytest.raises(ConflictError):
        repository.create(body.model_copy(update={"size_bytes": 456}), now)
    with pytest.raises(ConflictError):
        repository.create(body.model_copy(update={"release_id": uuid4()}), now)
    assert len(repository.list(0, 1)) == 1
    assert repository.list(1, 1) == []


def test_cleanup_only_published_staging_and_retries_after_failure(repository):
    now = datetime.now(UTC)
    objects = MagicMock()
    service = ReleaseService(repository, objects, lambda: now)
    release = service.create(release_input())
    assert not service.cleanup_once()
    repository.enqueue(release.release_id, 1)
    assert not service.cleanup_once()
    assert service.verify_once()
    assert not service.cleanup_once()
    formal_key = repository.object_key(release.release_id)[1]
    now += timedelta(hours=2)
    objects.delete.side_effect = OSError("unavailable")
    assert service.cleanup_once()
    assert not service.cleanup_once()
    now += timedelta(minutes=15)
    objects.delete.side_effect = None
    assert service.cleanup_once()
    assert objects.delete.call_args.args == (f"linux-release-uploads/{release.release_id}",)
    assert repository.object_key(release.release_id)[1] == formal_key
    assert not service.cleanup_once()
    now += timedelta(days=1)
    assert service.cleanup_once()


def test_publication_hash_verification_and_gc_reference(repository):
    now, objects = datetime.now(UTC), MagicMock()
    service = ReleaseService(repository, objects, lambda: now)
    release = service.create(release_input())
    repository.enqueue(release.release_id, 1)
    with pytest.raises(ConflictError):
        service.upload(release.release_id)
    assert service.verify_once()
    published, key = repository.object_key(release.release_id)
    assert published.state == "published"
    objects.finalize_artifact.assert_called_once()
    assert objects.finalize_artifact.call_args.args[1] == key
    with repository.sessions() as session:
        assert not session.scalar(
            select(BlobObjectRow.id).where(
                BlobObjectRow.id == published.blob_id,
                *no_blob_references(),
            )
        )
    assert not service.verify_once()


def test_interrupted_verifier_cannot_publish_after_reclaim(repository):
    now = datetime.now(UTC)
    release = repository.create(release_input(), now)
    repository.enqueue(release.release_id, 1)
    old = repository.claim(now)
    assert old
    assert repository.claim(now + timedelta(minutes=30)) is None
    interrupted = repository.get(release.release_id)
    assert interrupted.error_code == "verification_interrupted"
    repository.enqueue(release.release_id, interrupted.row_version)
    fresh = repository.claim(now + timedelta(minutes=31))
    assert fresh and fresh.candidate_blob_id != old.candidate_blob_id
    assert not repository.finish(old, now + timedelta(minutes=31))
    assert repository.finish(fresh, now + timedelta(minutes=32))
    assert repository.get(release.release_id).blob_id == fresh.candidate_blob_id


def test_bad_upload_never_publishes_and_error_is_sanitized(repository):
    now, objects = datetime.now(UTC), MagicMock()
    objects.finalize_artifact.side_effect = RuntimeError("secret-presigned-url")
    service = ReleaseService(repository, objects, lambda: now)
    release = service.create(release_input())
    repository.enqueue(release.release_id, 1)
    assert service.verify_once()
    failed = repository.get(release.release_id)
    assert failed.state == "failed" and failed.blob_id is None
    assert failed.error_code == "release_verification_failed"


def test_read_bounds_and_reserved_tag():
    repository, objects = MagicMock(), MagicMock()
    service = ReleaseService(repository, objects)
    with pytest.raises(InvalidRequestError):
        service.create(release_input(candidate_image="al1s-terminal-next:stable"))
    repository.object_key.return_value = (release_input(), "immutable-key")
    for start, end in [(-1, 10), (0, 123), (1, 0), (0, 4 * 1024**2)]:
        with pytest.raises(InvalidRequestError):
            service.read_range(uuid4(), start, end)
    objects.get_range.assert_not_called()


def test_admin_and_host_credentials_are_separate():
    terminal = uuid4()
    settings = Settings(
        admin_password="release-test-password",
        admin_cookie_secure=False,
        host_managers={terminal: HostManagerConnection(url="https://host.local", token="x" * 32)},
    )
    app = FastAPI()
    app.state.settings, app.state.execution_resources = settings, MagicMock()
    app.state.linux_releases = MagicMock()
    install_admin_auth(app, settings)
    app.include_router(router, prefix="/api/v1")

    @app.exception_handler(ExecutionDomainError)
    async def error(_request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)

    client = TestClient(app)
    assert client.get("/api/v1/releases/linux").status_code == 401
    path = f"/api/v1/terminal/host-upgrades/{terminal}/releases/{uuid4()}"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    client.post(
        "/api/v1/auth/login",
        json={"password": "release-test-password"},
        headers={"X-AL1S-CSRF": "1"},
    )
    assert client.get(path).status_code == 401  # Admin cookie is not host identity.
    assert client.post("/api/v1/releases/linux", json={}).status_code == 403
    app.state.execution_resources.require_linux_terminal.assert_not_called()


def test_ready_blob_reuse_and_expired_completion_are_fenced(repository):
    now = datetime.now(UTC)
    first = repository.create(release_input(), now)
    repository.enqueue(first.release_id, 1)
    job = repository.claim(now)
    assert repository.finish(job, now)
    second = repository.create(release_input(), now)
    repository.enqueue(second.release_id, 1)
    next_job = repository.claim(now)
    assert repository.finish(next_job, now)
    assert repository.get(second.release_id).blob_id == repository.get(first.release_id).blob_id
    third = repository.create(release_input(sha256="c" * 64), now)
    repository.enqueue(third.release_id, 1)
    late = repository.claim(now)
    assert not repository.finish(late, now + timedelta(minutes=30))
    assert repository.get(third.release_id).state == "failed"


def test_upgrade_deadline_and_release_identity(repository):
    from al1s.adapters.postgres.execution_models import TerminalRow
    from al1s.adapters.postgres.maintenance_repository import MaintenanceRepository
    from al1s.execution.host_management import HostCommand
    from al1s.execution.maintenance_types import MaintenanceRecord

    now, terminal, identity = datetime.now(UTC), uuid4(), uuid4()
    release = repository.create(release_input(), now)
    with repository.sessions.begin() as session:
        session.add(
            TerminalRow(
                id=terminal,
                installation_id=uuid4(),
                terminal_type="linux",
                display_name="upgrade-test",
                service_status="online",
                acceptance_status="accepting",
                agent_version="test",
                row_version=1,
                last_seen_at=now,
            )
        )
    maintenance = MaintenanceRepository(repository.sessions)
    record = MaintenanceRecord(
        identity,
        terminal,
        "upgrade_container",
        "a" * 64,
        "pending",
        now,
        now + timedelta(seconds=600),
        None,
        None,
        None,
        0,
        None,
        release.release_id,
    )
    maintenance.reserve(record)
    remote = HostCommand(
        command_id=identity,
        action="upgrade_container",
        release_id=release.release_id,
        state="executing",
        accepted_at=now.timestamp(),
        started_at=now.timestamp(),
        expires_at=(now + timedelta(seconds=600)).timestamp(),
        error_code=None,
        version=2,
    )
    assert (
        maintenance.observe(terminal, identity, now + timedelta(seconds=601), remote).state
        == "executing"
    )
    assert (
        maintenance.observe(terminal, identity, now + timedelta(seconds=1800)).state
        == "recovery_timeout"
    )
    late = remote.model_copy(update={"state": "succeeded", "version": 3})
    observed = maintenance.observe(terminal, identity, now + timedelta(seconds=1801), late)
    assert observed.state == "recovery_timeout" and observed.late_state == "succeeded"
    with pytest.raises(ConflictError):
        maintenance.observe(
            terminal, identity, now, late.model_copy(update={"release_id": uuid4()})
        )
