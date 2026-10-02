import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, insert, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.editor_repository import EditorSqlTransaction
from al1s.adapters.postgres.execution_models import (
    TargetDeviceRow,
    TerminalCapabilityProfileRow,
    TerminalRow,
)
from al1s.adapters.postgres.execution_repositories import PostgresTargetDeviceRepository
from al1s.api.editor_sessions import router
from al1s.execution.editor_service import EditorSessionService
from al1s.execution.editor_sessions import EditorStatus
from al1s.execution.errors import ConflictError, ExecutionDomainError
from al1s.execution.types import TargetDeviceMode
from al1s.secrets.security import FernetSecretCipher

pytestmark = pytest.mark.integration


@pytest.fixture
def environment():
    url = os.getenv("AL1S_EDITOR_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated editor test database required")
    assert make_url(url).database == "al1s_editor_test", "refuse non-isolated database"
    engine = create_engine(url)
    terminal, device = uuid4(), uuid4()
    profile = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(TerminalRow).values(
                id=terminal,
                installation_id=uuid4(),
                terminal_type="linux",
                display_name="isolated",
                agent_version="test",
            )
        )
        connection.execute(
            insert(TerminalCapabilityProfileRow).values(
                id=profile,
                terminal_id=terminal,
                revision=1,
                schema_version=1,
                protocol_version=1,
                agent_version="test",
                os_name="linux",
                os_version="test",
                architecture="arm64",
                cpu_cores=4,
                memory_bytes=1024**3,
                storage_available_bytes=1024**3,
                low_resource=False,
                provider_keys=["editor-session-v1"],
                details={},
                manifest_hash="a" * 64,
            )
        )
        connection.execute(
            update(TerminalRow)
            .where(TerminalRow.id == terminal)
            .values(current_capability_profile_id=profile)
        )
        connection.execute(
            insert(TargetDeviceRow).values(
                id=device, display_name="isolated", mode="mounted", managing_terminal_id=terminal
            )
        )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    clock = [datetime.now(UTC)]
    service = EditorSessionService(
        lambda: EditorSqlTransaction(sessions),
        FernetSecretCipher("isolated-editor-master-key" * 3),
        lambda: clock[0],
    )
    yield service, terminal, device, clock, engine
    engine.dispose()


def test_sql_creation_replay_deadline_and_concurrency(environment):
    service, terminal, device, clock, _ = environment
    request = uuid4()
    first = service.create(device, request)
    assert service.create(device, request) == first
    clock[0] += timedelta(seconds=30)
    second = service.create(device, uuid4())
    assert second.session_id != first.session_id
    assert service.detail(first.session_id)[0].status is EditorStatus.EXPIRED
    service.close(second.session_id)
    barrier = Barrier(2)

    def create():
        barrier.wait()
        try:
            return service.create(device, uuid4()).status
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert sorted(results) == ["conflict", "pending"]
    assert len(service.list_terminal(terminal)) == 1


def test_mode_change_waits_for_editor_release(environment):
    service, _terminal, device, clock, engine = environment
    editor = service.create(device, uuid4())
    sessions = sessionmaker(bind=engine)
    with sessions.begin() as session:
        repo = PostgresTargetDeviceRepository(session)
        target = repo.get_active(device, for_update=True)
        assert (
            repo.update_mode(
                device, target.row_version, TargetDeviceMode.UNASSIGNED, None, clock[0]
            )
            is None
        )
    service.close(editor.session_id)
    with sessions.begin() as session:
        repo = PostgresTargetDeviceRepository(session)
        target = repo.get_active(device, for_update=True)
        assert (
            repo.update_mode(
                device, target.row_version, TargetDeviceMode.UNASSIGNED, None, clock[0]
            )
            is not None
        )


def test_http_summary_never_exposes_secret_and_uses_bounded_query(environment):
    service, terminal, device, _, engine = environment
    app = FastAPI()
    app.state.editor_sessions = service
    app.include_router(router)

    @app.exception_handler(ExecutionDomainError)
    async def domain_error(request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)

    with TestClient(app) as client:
        result = client.post(
            "/editor-sessions",
            json={"device_id": str(device)},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert result.status_code == 201
        identifier = result.json()["session_id"]
        assert "ciphertext" not in result.text and result.headers["cache-control"] == "no-store"
        assert (
            client.get(
                f"/editor-sessions/{identifier}", headers={"Authorization": "Bearer test"}
            ).status_code
            == 403
        )
        assert client.delete(f"/editor-sessions/{identifier}").json()["status"] == "closed"

    queries = []

    def count(*args):
        queries.append(args[2])

    event.listen(engine, "before_cursor_execute", count)
    try:
        assert service.list_terminal(terminal, 20) == ()
        assert len(queries) == 1 and "LIMIT" in queries[0]
    finally:
        event.remove(engine, "before_cursor_execute", count)
