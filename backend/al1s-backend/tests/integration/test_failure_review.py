from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.artifact_models import TerminalArtifactRow
from al1s.adapters.postgres.delivery_models import TerminalReportRow
from al1s.adapters.postgres.failure_review import PostgresFailureReview
from al1s.adapters.postgres.models import BlobObjectRow, GcJobRow
from al1s.adapters.postgres.scheduling_models import ExecutionAttemptRow
from al1s.adapters.postgres.unit_of_work import SqlAlchemyUnitOfWork
from al1s.api.task_failure_details import router
from al1s.execution.delivery_types import PackageReceiptDisposition, TerminalResultKind
from al1s.execution.errors import ExecutionDomainError
from al1s.execution.failure_review_service import FailureReviewService
from al1s.kernel.gc_worker import GcWorker
from tests.integration.execution_support import (
    _delivery_services,
    _prepare_terminal,
    _services,
    _single_command,
)
from tests.integration.execution_support import (
    clean_tables as _clean_tables,
)
from tests.integration.execution_support import (
    engine as _engine,
)

pytestmark = pytest.mark.integration
engine = _engine
clean_tables = _clean_tables


@pytest.fixture(autouse=True)
def clean_blobs(engine, clean_tables):
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE blob_objects CASCADE"))
    yield
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE blob_objects CASCADE"))


class Store:
    body = b"unchanged screenshot"

    def __init__(self):
        self.deleted = []

    def get_range(self, key, start, end):
        return self.body[start : end + 1]

    def delete(self, key):
        self.deleted.append(key)


def setup_failure(engine, *, no_image=False, local_identity=False):
    now = datetime(2026, 9, 9, tzinfo=UTC)
    resource, scheduling = _services(engine, [now])
    delivery, _ = _delivery_services(engine, [now])
    terminal, target = _prepare_terminal(resource)
    task = scheduling.create_task(_single_command(target), correlation_id=uuid4())
    scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())
    (command,) = delivery.list_commands(terminal)
    receipt = delivery.receive_package(
        terminal_id=terminal,
        package_id=command.package_id,
        attempt_id=command.attempt_id,
        report_id=uuid4(),
        command_id=command.command_id,
        disposition=PackageReceiptDisposition.ACCEPTED,
        rejection_code=None,
        diagnostic=None,
        occurred_at=now,
        correlation_id=uuid4(),
    )
    start = delivery.start_attempt(
        terminal_id=terminal,
        attempt_id=command.attempt_id,
        package_id=command.package_id,
        offline_permit_id=receipt.offline_start_permit.permit_id,
        offline_permit_token=receipt.offline_start_permit.token,
        report_id=uuid4(),
        occurred_at=now,
        correlation_id=uuid4(),
    )
    artifact, blob = uuid4(), uuid4()
    capture = uuid4() if local_identity else artifact
    diagnostic = {
        "modules": [
            {
                "module_index": 3,
                "definition_key": "script-1",
                "result": {
                    "success": False,
                    "failed_step": {"number": 2},
                    "failure_diagnosis": {"title": "recognition", "message": "not matched"},
                    **(
                        {"failure_screenshot_error": "device disconnected"}
                        if no_image
                        else {
                            "failure_screenshot": {"artifact_id": str(capture)},
                        }
                    ),
                },
            }
        ]
    }
    delivery.receive_result(
        terminal_id=terminal,
        attempt_id=command.attempt_id,
        package_id=command.package_id,
        report_id=uuid4(),
        lease_id=start.lease.lease_id,
        lease_version=start.lease.row_version,
        result_kind=TerminalResultKind.FAILURE,
        error_code="maa_pipeline_failed",
        retryable=False,
        diagnostic=diagnostic,
        occurred_at=now,
        correlation_id=uuid4(),
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    if not no_image:
        with sessions.begin() as session:
            session.add(
                BlobObjectRow(
                    id=blob,
                    sha256=sha256(Store.body).hexdigest(),
                    size_bytes=len(Store.body),
                    media_type="image/png",
                    object_key=f"test/{blob}",
                    status="ready",
                    ready_at=now,
                )
            )
            session.flush()
            session.add(
                TerminalArtifactRow(
                    id=artifact,
                    terminal_id=terminal,
                    owner_kind="formal_attempt",
                    owner_id=command.attempt_id,
                    artifact_kind="screenshot",
                    file_name="测试脚本-第02步.png",
                    expected_sha256=sha256(Store.body).hexdigest(),
                    expected_size_bytes=len(Store.body),
                    media_type="image/png",
                    object_key=f"artifact/{artifact}",
                    status="ready",
                    idempotency_key=str(capture),
                    expires_at=now + timedelta(hours=1),
                    completed_at=now,
                    blob_id=blob,
                    created_at=now,
                )
            )
    store = Store()
    repository = PostgresFailureReview(sessions)
    app = FastAPI()
    app.state.failure_reviews = FailureReviewService(repository, store, lambda: now)
    app.include_router(router, prefix="/tasks")

    @app.exception_handler(ExecutionDomainError)
    async def error_handler(request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)

    return (
        TestClient(app),
        task.task.task_id,
        command.attempt_id,
        capture,
        blob,
        sessions,
        repository,
        store,
        now,
    )


@pytest.mark.parametrize("local_identity", [False, True])
def test_full_http_failure_download_confirm_gc(engine, local_identity):
    client, task, attempt, image, blob, sessions, _repository, store, now = setup_failure(
        engine, local_identity=local_identity
    )
    base = f"/tasks/{task}/attempts/{attempt}"
    data = client.get(f"/tasks/{task}/details").json()
    detail = data["items"][0]["details"][0]
    assert detail["module_number"] == 3 and detail["step_number"] == 2
    assert "object_key" not in str(data)
    assert client.post(base + "/confirm-failure").status_code == 409
    assert client.get(base + f"/screenshots/{image}?preview=true").content == store.body
    assert client.post(base + "/confirm-failure").status_code == 409
    assert (
        client.get(
            base + f"/screenshots/{image}", headers={"Authorization": "Bearer terminal"}
        ).status_code
        == 403
    )
    assert client.get(f"/tasks/{uuid4()}/attempts/{attempt}/screenshots/{image}").status_code == 404
    response = client.get(base + f"/screenshots/{image}")
    assert response.content == store.body
    assert "filename*=UTF-8" in response.headers["content-disposition"]
    assert client.post(base + "/confirm-failure").status_code == 204
    assert client.post(base + "/confirm-failure").status_code == 204
    assert client.get(base + f"/screenshots/{image}").status_code == 404
    assert client.get(f"/tasks/{task}/details").json()["items"][0]["confirmed"]
    worker = GcWorker(
        uow_factory=lambda: SqlAlchemyUnitOfWork(sessions),
        blob_store=store,
        worker_id="test",
        now=lambda: now,
    )
    assert worker.run_once().deleted == 1
    assert store.deleted == [f"test/{blob}"]


def test_notification_evidence_resolves_local_identity_and_respects_owner_and_confirmation(engine):
    from al1s.adapters.postgres.notification_evidence import PostgresNotificationEvidence

    client, task, attempt, image, _, sessions, _, _, now = setup_failure(engine)
    local_id = uuid4()
    with sessions.begin() as session:
        artifact = session.get(TerminalArtifactRow, image)
        terminal = artifact.terminal_id
        artifact.idempotency_key = str(local_id)
        execution = session.get(ExecutionAttemptRow, attempt).execution_id
    reader = PostgresNotificationEvidence(sessions, now=lambda: now)
    expected = reader.read(task, execution, attempt, terminal, image)
    assert expected.status == "ready"
    assert reader.read(task, execution, attempt, terminal, local_id) == expected
    assert reader.read(task, execution, attempt, uuid4(), image).status == "unavailable"
    assert reader.read(uuid4(), execution, attempt, terminal, image).status == "unavailable"
    assert reader.read(task, execution, attempt, terminal, uuid4()).status == "pending"
    base = f"/tasks/{task}/attempts/{attempt}"
    assert client.get(base + f"/screenshots/{image}").status_code == 200
    assert client.post(base + "/confirm-failure").status_code == 204
    assert reader.read(task, execution, attempt, terminal, local_id).status == "unavailable"


def test_successful_task_screenshot_download_and_retention(engine):
    client, task, attempt, image, blob, sessions, repository, store, now = setup_failure(
        engine, local_identity=True
    )
    with sessions.begin() as session:
        session.get(ExecutionAttemptRow, attempt).result = "success"
        report = session.scalar(
            select(TerminalReportRow).where(
                TerminalReportRow.attempt_id == attempt,
                TerminalReportRow.report_kind == "attempt_result",
            )
        )
        report.result_diagnostic = {
            "modules": [
                {
                    "result": {
                        "success": True,
                        "custom_actions": [
                            {
                                "action": "screenshot",
                                "result": {
                                    "artifact_id": str(image),
                                    "artifact_kind": "screenshot",
                                    "file_name": "step.png",
                                },
                            },
                        ],
                    }
                }
            ]
        }
    base = f"/tasks/{task}/attempts/{attempt}"
    details = client.get(f"/tasks/{task}/details").json()["items"][0]
    assert details["details"] == []
    assert details["screenshots"] == [{"artifact_id": str(image), "file_name": "step.png"}]
    assert client.get(base + f"/screenshots/{image}").content == store.body
    assert client.post(base + "/confirm-failure").status_code == 404
    assert repository.expire(now + timedelta(days=31)) == 1
    with sessions() as session:
        assert session.scalar(select(GcJobRow).where(GcJobRow.blob_id == blob)) is not None


@pytest.mark.parametrize("state", ["pending", "expired"])
def test_explicit_upload_expiry_allows_no_image_review_but_pending_does_not(engine, state):
    client, task, attempt, image, _, sessions, _, _, now = setup_failure(
        engine, local_identity=True
    )
    with sessions.begin() as session:
        artifact = session.scalar(
            select(TerminalArtifactRow).where(TerminalArtifactRow.owner_id == attempt)
        )
        artifact.status = state
        artifact.blob_id = None
        artifact.completed_at = now if state == "expired" else None
    item = client.get(f"/tasks/{task}/details").json()["items"][0]
    assert item["no_screenshot"] is (state == "expired")
    if state == "expired":
        assert item["details"][0]["screenshot_error"]
        assert item["screenshots"] == []
    else:
        assert item["details"][0]["screenshot_id"] == str(image)
    response = client.post(f"/tasks/{task}/attempts/{attempt}/confirm-failure")
    assert response.status_code == (204 if state == "expired" else 409)


def test_no_image_and_expiry(engine):
    client, task, attempt, _, _, sessions, _repository, _store, _now = setup_failure(
        engine, no_image=True
    )
    assert client.post(f"/tasks/{task}/attempts/{attempt}/confirm-failure").status_code == 204
    with sessions() as session:
        assert (
            session.scalar(
                select(TerminalReportRow).where(TerminalReportRow.report_kind == "attempt_result")
            ).result_diagnostic
            is None
        )


@pytest.mark.parametrize("local_identity", [False, True])
def test_retention_releases_unconfirmed_without_faking_confirmation(engine, local_identity):
    _client, _task, attempt, _image, blob, sessions, repository, _store, now = setup_failure(
        engine, local_identity=local_identity
    )
    assert repository.expire(now + timedelta(days=31)) == 1
    with sessions() as session:
        report = session.scalar(
            select(TerminalReportRow).where(TerminalReportRow.report_kind == "attempt_result")
        )
        assert report.detail_confirmed_at is None and report.result_diagnostic is None
        assert (
            session.scalar(
                select(TerminalArtifactRow).where(TerminalArtifactRow.owner_id == attempt)
            ).blob_id
            is None
        )
        assert session.scalar(select(GcJobRow).where(GcJobRow.blob_id == blob)) is not None


def test_shared_blob_is_not_deleted_and_deleted_blob_cannot_gain_reference(engine):
    client, task, attempt, image, blob, sessions, _repository, store, now = setup_failure(engine)
    other = uuid4()
    with sessions.begin() as session:
        source = session.get(TerminalArtifactRow, image)
        session.add(
            TerminalArtifactRow(
                id=other,
                terminal_id=source.terminal_id,
                owner_kind="formal_attempt",
                owner_id=attempt,
                artifact_kind="screenshot",
                file_name="other.png",
                expected_sha256=source.expected_sha256,
                expected_size_bytes=source.expected_size_bytes,
                media_type="image/png",
                object_key=f"artifact/{other}",
                status="ready",
                idempotency_key=str(other),
                expires_at=source.expires_at,
                completed_at=now,
                blob_id=blob,
                created_at=now,
            )
        )
    base = f"/tasks/{task}/attempts/{attempt}"
    assert client.get(base + f"/screenshots/{image}").status_code == 200
    assert client.post(base + "/confirm-failure").status_code == 204
    clock = [now]
    worker = GcWorker(
        uow_factory=lambda: SqlAlchemyUnitOfWork(sessions),
        blob_store=store,
        worker_id="test",
        now=lambda: clock[0],
    )
    assert worker.run_once().deleted == 0
    assert store.deleted == []
    with sessions.begin() as session:
        item = session.get(TerminalArtifactRow, other)
        item.status, item.blob_id = "expired", None
    clock[0] += timedelta(minutes=10)
    assert worker.run_once().deleted == 1
    with pytest.raises(IntegrityError), sessions.begin() as session:
        item = session.get(TerminalArtifactRow, other)
        item.status, item.blob_id = "ready", blob
        session.flush()


def test_details_are_paged_with_at_most_three_queries_not_per_attempt(engine):
    _client, task, attempt, _image, _blob, sessions, repository, _store, now = setup_failure(engine)
    with sessions.begin() as session:
        source = session.get(ExecutionAttemptRow, attempt)
        session.add_all(
            [
                ExecutionAttemptRow(
                    execution_id=source.execution_id,
                    attempt_no=number,
                    status="ended",
                    result="failure",
                    available_at=now,
                    enqueued_at=now,
                    ended_at=now,
                )
                for number in range(2, 25)
            ]
        )
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        first = repository.list_attempts(task, None, now)
        # Attempts, accepted reports, and one optional batch of screenshot states.
        expected = 3 if any(item["attempt_id"] == attempt for item in first["items"]) else 2
        assert len(statements) == expected
        statements.clear()
        second = repository.list_attempts(task, first["next_cursor"], now)
        expected = 3 if any(item["attempt_id"] == attempt for item in second["items"]) else 2
        assert len(statements) == expected
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(first["items"]) == 20 and len(second["items"]) == 4
    assert second["next_cursor"] is None
    assert not (
        {item["attempt_id"] for item in first["items"]}
        & {item["attempt_id"] for item in second["items"]}
    )


def test_downgrade_cannot_erase_confirmation(engine, monkeypatch):
    from importlib import import_module

    client, task, attempt, _, _, _sessions, _repository, _store, _now = setup_failure(
        engine, no_image=True
    )
    assert client.post(f"/tasks/{task}/attempts/{attempt}/confirm-failure").status_code == 204
    revision = import_module("migrations.versions.20260909_0024_failure_review")
    with engine.connect() as connection:
        monkeypatch.setattr(revision.op, "get_bind", lambda: connection)
        with pytest.raises(RuntimeError, match="Cannot discard"):
            revision.downgrade()
