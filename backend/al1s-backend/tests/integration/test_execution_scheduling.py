from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine, event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from al1s.adapters.postgres.scheduling_models import (
    ExecutionAttemptRow,
    ExecutionRow,
    ExecutionSnapshotRow,
    PlanOccurrenceRow,
    StateTransitionRow,
    TaskRequestRow,
    TaskRetryOriginRow,
    TaskScheduleRevisionRow,
    TaskScheduleRevisionTimeRow,
    TaskScheduleRow,
)
from al1s.app.config import Settings
from al1s.app.factory import create_app
from al1s.execution.errors import ConflictError, NotFoundError
from al1s.execution.scheduling_types import (
    CreateTaskCommand,
    ExecutionResult,
    LoopScheduleSpec,
    OccurrenceStatus,
    RetryOriginalSnapshotCommand,
    ScheduleStatus,
    TaskLifecycleStatus,
    TaskType,
    TimedScheduleSpec,
    UpdateTimedScheduleCommand,
)
from al1s.infrastructure.readiness import ReadinessResult
from tests.integration.execution_support import (
    FakeDefinitionProvider,
    _loop_command,
    _prepare_terminal,
    _services,
    _single_command,
    _timed_command,
)
from tests.integration.execution_support import (
    clean_tables as clean_tables,
)
from tests.integration.execution_support import (
    engine as engine,
)

pytestmark = pytest.mark.integration


def test_task_creation_is_idempotent_and_builds_stable_occurrences(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 8, 29, 2, 0, tzinfo=UTC)]
    provider = FakeDefinitionProvider()
    resource_service, scheduling = _services(engine, clock, provider)
    _, target_id = _prepare_terminal(resource_service)
    command = _loop_command(target_id)

    created = scheduling.create_task(command, correlation_id=uuid4())
    provider.available = False
    replay = scheduling.create_task(command, correlation_id=uuid4())

    assert replay.task.task_id == created.task.task_id
    assert provider.resolve_count == 1
    assert created.schedule is not None
    assert [item.ordinal for item in created.occurrences] == [1, 2]
    with pytest.raises(ConflictError, match="different task request"):
        scheduling.create_task(
            replace(command, name="changed"),
            correlation_id=uuid4(),
        )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TaskRequestRow)) == 1
        assert session.scalar(select(func.count()).select_from(PlanOccurrenceRow)) == 2
        assert session.scalar(select(func.count()).select_from(StateTransitionRow)) == 2


def test_loop_materialization_retries_same_snapshot_then_opens_next_occurrence(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 8, 29, 2, 0, tzinfo=UTC)]
    provider = FakeDefinitionProvider()
    resource_service, scheduling = _services(engine, clock, provider)
    _, target_id = _prepare_terminal(resource_service)
    created = scheduling.create_task(_loop_command(target_id), correlation_id=uuid4())
    worker_id = uuid4()

    first_batch = scheduling.materialize_due(worker_id=worker_id, correlation_id=uuid4())
    assert len(first_batch) == 1
    assert scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4()) == []
    first = first_batch[0]
    scheduling.start_attempt(first.attempt.attempt_id, correlation_id=uuid4())
    failed = scheduling.complete_attempt(
        first.attempt.attempt_id,
        result=ExecutionResult.FAILURE,
        error_code="image_not_found",
        retryable=True,
        correlation_id=uuid4(),
    )
    assert failed.retry_attempt is not None
    retrying_summary = scheduling.list_active_schedules(limit=100)[0]
    assert retrying_summary.schedule.settled_occurrences == 0
    provider.revision_id = "revision-2"
    scheduling.start_attempt(failed.retry_attempt.attempt_id, correlation_id=uuid4())
    scheduling.complete_attempt(
        failed.retry_attempt.attempt_id,
        result=ExecutionResult.SUCCESS,
        error_code=None,
        retryable=False,
        correlation_id=uuid4(),
    )

    first_settled_summary = scheduling.list_active_schedules(limit=100)[0]
    assert first_settled_summary.schedule.settled_occurrences == 1

    second_batch = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())
    assert len(second_batch) == 1
    second = second_batch[0]
    scheduling.start_attempt(second.attempt.attempt_id, correlation_id=uuid4())
    scheduling.complete_attempt(
        second.attempt.attempt_id,
        result=ExecutionResult.SUCCESS,
        error_code=None,
        retryable=False,
        correlation_id=uuid4(),
    )

    settled = scheduling.get_task(created.task.task_id)
    assert settled.task.lifecycle_status is TaskLifecycleStatus.COMPLETED
    assert settled.schedule is not None
    assert settled.schedule.status is ScheduleStatus.COMPLETED
    with Session(engine) as session:
        snapshots = session.scalars(select(ExecutionSnapshotRow)).all()
        attempts = session.scalars(
            select(ExecutionAttemptRow).order_by(ExecutionAttemptRow.enqueued_at)
        ).all()
        assert len(snapshots) == 2
        first_snapshot = session.get(ExecutionSnapshotRow, first.snapshot.snapshot_id)
        second_snapshot = session.get(ExecutionSnapshotRow, second.snapshot.snapshot_id)
        assert first_snapshot is not None
        assert second_snapshot is not None
        assert first_snapshot.revision_id == "revision-1"
        assert second_snapshot.revision_id == "revision-2"
        assert len(attempts) == 3
        assert attempts[0].execution_id == attempts[1].execution_id


def test_pause_and_terminate_do_not_retry_the_running_attempt(engine: Engine) -> None:
    clock = [datetime(2026, 8, 29, 2, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    _, target_id = _prepare_terminal(resource_service)

    paused_task = scheduling.create_task(
        _loop_command(target_id, key="pause-running-key"), correlation_id=uuid4()
    )
    assert paused_task.schedule is not None
    paused_execution = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
    scheduling.start_attempt(paused_execution.attempt.attempt_id, correlation_id=uuid4())
    paused_schedule = scheduling.pause_schedule(
        paused_task.schedule.schedule_id,
        expected_version=paused_task.schedule.row_version,
        correlation_id=uuid4(),
    )
    paused_completion = scheduling.complete_attempt(
        paused_execution.attempt.attempt_id,
        result=ExecutionResult.FAILURE,
        error_code="retryable_failure",
        retryable=True,
        correlation_id=uuid4(),
    )
    assert paused_completion.retry_attempt is None
    assert paused_completion.execution.result is ExecutionResult.FAILURE
    assert scheduling.get_task(paused_task.task.task_id).schedule == paused_schedule

    terminated_task = scheduling.create_task(
        _loop_command(target_id, key="terminate-running-key"), correlation_id=uuid4()
    )
    assert terminated_task.schedule is not None
    terminated_execution = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
    scheduling.start_attempt(terminated_execution.attempt.attempt_id, correlation_id=uuid4())
    terminating = scheduling.terminate_schedule(
        terminated_task.schedule.schedule_id,
        expected_version=terminated_task.schedule.row_version,
        correlation_id=uuid4(),
    )
    assert terminating.status is ScheduleStatus.TERMINATING
    terminated_completion = scheduling.complete_attempt(
        terminated_execution.attempt.attempt_id,
        result=ExecutionResult.FAILURE,
        error_code="retryable_failure",
        retryable=True,
        correlation_id=uuid4(),
    )
    assert terminated_completion.retry_attempt is None
    settled = scheduling.get_task(terminated_task.task.task_id)
    assert settled.task.lifecycle_status is TaskLifecycleStatus.TERMINATED
    assert settled.schedule is not None
    assert settled.schedule.status is ScheduleStatus.TERMINATED


def test_timed_pause_and_resume_skip_due_occurrences_without_catchup(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 8, 29, 2, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    _, target_id = _prepare_terminal(resource_service)
    command = CreateTaskCommand(
        idempotency_key="timed-key",
        name="timed test",
        task_type=TaskType.TIMED,
        source_module="test",
        logical_content_id="strategy-1",
        parameters={},
        requested_terminal_id=None,
        requested_target_device_id=target_id,
        timeout_seconds=300,
        max_retries=0,
        record_video=False,
        timed=TimedScheduleSpec(
            timezone="Asia/Shanghai",
            start_date=date(2026, 8, 29),
            end_date=date(2026, 8, 29),
            daily_times=(time(9, 0), time(12, 0)),
        ),
    )
    created = scheduling.create_task(command, correlation_id=uuid4())
    assert created.schedule is not None

    paused = scheduling.pause_schedule(
        created.schedule.schedule_id,
        expected_version=created.schedule.row_version,
        correlation_id=uuid4(),
    )
    clock[0] = datetime(2026, 8, 29, 5, 0, tzinfo=UTC)
    resumed = scheduling.resume_schedule(
        paused.schedule_id,
        expected_version=paused.row_version,
        correlation_id=uuid4(),
    )

    assert resumed.status is ScheduleStatus.COMPLETED
    assert scheduling.get_task(created.task.task_id).task.lifecycle_status is (
        TaskLifecycleStatus.COMPLETED
    )
    assert scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4()) == []
    with Session(engine) as session:
        statuses = session.scalars(
            select(PlanOccurrenceRow.status).order_by(PlanOccurrenceRow.ordinal)
        ).all()
        assert statuses == ["skipped", "skipped"]


def test_timed_schedule_revision_preserves_history_and_recreates_removed_time(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 8, 29, 2, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    _, target_id = _prepare_terminal(resource_service)
    created = scheduling.create_task(_timed_command(target_id), correlation_id=uuid4())
    assert created.schedule is not None
    original_by_time = {item.scheduled_for: item for item in created.occurrences}
    noon = datetime(2026, 8, 29, 4, 0, tzinfo=UTC)
    three_pm = datetime(2026, 8, 29, 7, 0, tzinfo=UTC)
    six_pm = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)

    revised = scheduling.revise_timed_schedule(
        created.schedule.schedule_id,
        UpdateTimedScheduleCommand(
            expected_version=created.schedule.row_version,
            start_date=date(2026, 8, 29),
            end_date=date(2026, 8, 29),
            daily_times=(time(12, 0), time(18, 0)),
        ),
        correlation_id=uuid4(),
    )
    assert revised.changed is True
    assert revised.revision.revision == 2
    assert revised.retained_occurrences == 1
    assert revised.cancelled_occurrences == 1
    assert [item.scheduled_for for item in revised.added_occurrences] == [six_pm]
    with pytest.raises(ConflictError, match="changed concurrently"):
        scheduling.revise_timed_schedule(
            revised.schedule.schedule_id,
            UpdateTimedScheduleCommand(
                expected_version=created.schedule.row_version,
                start_date=date(2026, 8, 29),
                end_date=date(2026, 8, 29),
                daily_times=(time(12, 0), time(18, 0)),
            ),
            correlation_id=uuid4(),
        )

    unchanged = scheduling.revise_timed_schedule(
        revised.schedule.schedule_id,
        UpdateTimedScheduleCommand(
            expected_version=revised.schedule.row_version,
            start_date=date(2026, 8, 29),
            end_date=date(2026, 8, 29),
            daily_times=(time(18, 0), time(12, 0), time(12, 0)),
        ),
        correlation_id=uuid4(),
    )
    assert unchanged.changed is False
    assert unchanged.schedule.row_version == revised.schedule.row_version

    restored = scheduling.revise_timed_schedule(
        revised.schedule.schedule_id,
        UpdateTimedScheduleCommand(
            expected_version=revised.schedule.row_version,
            start_date=date(2026, 8, 29),
            end_date=date(2026, 8, 29),
            daily_times=(time(12, 0), time(15, 0), time(18, 0)),
        ),
        correlation_id=uuid4(),
    )
    assert restored.revision.revision == 3
    assert restored.retained_occurrences == 2
    assert restored.cancelled_occurrences == 0
    assert [item.scheduled_for for item in restored.added_occurrences] == [three_pm]

    current_occurrences = scheduling.list_schedule_occurrences(
        restored.schedule.schedule_id,
        historical=False,
        limit=50,
    )
    assert [item.occurrence.scheduled_for for item in current_occurrences] == [
        noon,
        three_pm,
        six_pm,
    ]
    assert [item.display_ordinal for item in current_occurrences] == [1, 2, 3]
    assert [item.occurrence.ordinal for item in current_occurrences] == [1, 4, 3]

    historical_occurrences = scheduling.list_schedule_occurrences(
        restored.schedule.schedule_id,
        historical=True,
        limit=50,
    )
    assert len(historical_occurrences) == 1
    assert historical_occurrences[0].display_ordinal is None
    assert historical_occurrences[0].occurrence.ordinal == 2
    assert historical_occurrences[0].schedule_revision == 1

    active_summary = scheduling.list_active_schedules(limit=50)[0].schedule
    assert active_summary.total_occurrences == 3
    assert active_summary.current_occurrence_ordinal is None
    assert active_summary.next_occurrence_ordinal == 1

    with Session(engine) as session:
        revisions = session.scalars(
            select(TaskScheduleRevisionRow).order_by(TaskScheduleRevisionRow.revision)
        ).all()
        occurrences = session.scalars(
            select(PlanOccurrenceRow).order_by(
                PlanOccurrenceRow.scheduled_for, PlanOccurrenceRow.ordinal
            )
        ).all()
        assert [item.revision for item in revisions] == [1, 2, 3]
        assert session.scalar(select(func.count()).select_from(TaskScheduleRevisionTimeRow)) == 7
        noon_rows = [item for item in occurrences if item.scheduled_for == noon]
        three_pm_rows = [item for item in occurrences if item.scheduled_for == three_pm]
        assert len(noon_rows) == 1
        assert noon_rows[0].id == original_by_time[noon].occurrence_id
        assert len(three_pm_rows) == 2
        assert [item.status for item in three_pm_rows] == [
            OccurrenceStatus.CANCELLED.value,
            OccurrenceStatus.PLANNED.value,
        ]
        assert three_pm_rows[0].id == original_by_time[three_pm].occurrence_id
        assert three_pm_rows[0].schedule_revision_id == revisions[0].id
        assert three_pm_rows[1].schedule_revision_id == revisions[2].id

    with pytest.raises(DBAPIError), Session(engine) as session:
        session.execute(
            text("UPDATE task_schedule_revisions SET end_date=:end_date WHERE id=:id"),
            {"end_date": date(2026, 8, 30), "id": revisions[0].id},
        )
        session.commit()


def test_original_snapshot_retry_creates_new_task_and_keeps_failed_history(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 8, 29, 2, 0, tzinfo=UTC)]
    provider = FakeDefinitionProvider()
    resource_service, scheduling = _services(engine, clock, provider)
    _, target_id = _prepare_terminal(resource_service)
    source_task = scheduling.create_task(
        _single_command(target_id, key="retry-source"), correlation_id=uuid4()
    )
    source = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
    with pytest.raises(ConflictError, match="ended failed execution"):
        scheduling.retry_original_snapshot(
            source.execution.execution_id,
            RetryOriginalSnapshotCommand(idempotency_key="retry-too-early"),
            correlation_id=uuid4(),
        )
    scheduling.start_attempt(source.attempt.attempt_id, correlation_id=uuid4())
    scheduling.complete_attempt(
        source.attempt.attempt_id,
        result=ExecutionResult.FAILURE,
        error_code="image_not_found",
        retryable=False,
        correlation_id=uuid4(),
    )
    provider.available = False

    retried = scheduling.retry_original_snapshot(
        source.execution.execution_id,
        RetryOriginalSnapshotCommand(idempotency_key="retry-original-1"),
        correlation_id=uuid4(),
    )
    replay = scheduling.retry_original_snapshot(
        source.execution.execution_id,
        RetryOriginalSnapshotCommand(idempotency_key="retry-original-1"),
        correlation_id=uuid4(),
    )

    assert retried.task.task_id != source_task.task.task_id
    assert retried.execution.execution_id != source.execution.execution_id
    assert retried.snapshot.snapshot_id != source.snapshot.snapshot_id
    assert retried.snapshot.manifest_hash == source.snapshot.manifest_hash
    assert retried.snapshot.revision_id == source.snapshot.revision_id
    assert retried.origin.source_execution_id == source.execution.execution_id
    assert retried.origin.source_snapshot_id == source.snapshot.snapshot_id
    assert retried.task.name == "single test (原快照重试)"
    assert retried.occurrence.settled_at is None
    assert replay.task.task_id == retried.task.task_id
    assert replay.execution.execution_id == retried.execution.execution_id

    source_details = scheduling.get_execution_details(source.execution.execution_id)
    assert source_details.execution.status.value == "ended"
    assert source_details.execution.result is ExecutionResult.FAILURE
    assert source_details.attempts[0].error_code == "image_not_found"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TaskRequestRow)) == 2
        assert session.scalar(select(func.count()).select_from(TaskRetryOriginRow)) == 1

    with pytest.raises(ConflictError, match="different task request"):
        scheduling.retry_original_snapshot(
            source.execution.execution_id,
            RetryOriginalSnapshotCommand(idempotency_key="retry-original-1", name="different name"),
            correlation_id=uuid4(),
        )


def test_snapshot_rows_reject_updates_but_remain_retention_deletable(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 8, 29, 2, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    _, target_id = _prepare_terminal(resource_service)
    scheduling.create_task(
        replace(
            _loop_command(target_id, key="immutable-key"),
            loop=LoopScheduleSpec(1),
        ),
        correlation_id=uuid4(),
    )
    materialized = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())
    snapshot_id = materialized[0].snapshot.snapshot_id

    with pytest.raises(DBAPIError), Session(engine) as session:
        session.execute(
            text("UPDATE execution_snapshots SET revision_id='changed' WHERE id=:id"),
            {"id": snapshot_id},
        )
        session.commit()

    with Session(engine) as session:
        assert session.get(ExecutionSnapshotRow, snapshot_id) is not None
        assert session.scalar(select(func.count()).select_from(ExecutionRow)) == 1
        assert session.scalar(select(func.count()).select_from(TaskScheduleRow)) == 1


def test_task_history_and_active_schedule_queries_are_bounded_without_n_plus_one(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 8, 29, 2, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    _, target_id = _prepare_terminal(resource_service)
    for index in range(3):
        scheduling.create_task(
            _loop_command(target_id, key=f"active-{index}"), correlation_id=uuid4()
        )
    single = scheduling.create_task(
        _single_command(target_id, key="history-single"), correlation_id=uuid4()
    )

    query_count = 0

    def count_query(*_: object) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        active = scheduling.list_active_schedules(limit=100)
    finally:
        event.remove(engine, "before_cursor_execute", count_query)

    assert len(active) == 3
    assert query_count == 2
    assert all(item.schedule.planned_occurrences == 2 for item in active)
    assert all(item.schedule.settled_occurrences == 0 for item in active)
    assert all(item.pause.allowed for item in active)
    assert all(not item.resume.allowed for item in active)

    query_count = 0
    event.listen(engine, "before_cursor_execute", count_query)
    try:
        history = scheduling.list_task_history(limit=100)
    finally:
        event.remove(engine, "before_cursor_execute", count_query)

    assert [item.task.task_id for item in history] == [single.task.task_id]
    assert history[0].task.latest_result is None
    assert history[0].cancel.allowed
    assert not history[0].delete.allowed
    assert history[0].delete.refusal_code == "active_task_delete_forbidden"
    assert query_count == 1


def test_task_and_schedule_http_contract_use_separate_resource_routes(engine: Engine) -> None:
    class Ready:
        def check(self) -> ReadinessResult:
            return ReadinessResult(ready=True, dependencies={})

        def close(self) -> None:
            return

    clock = [datetime(2026, 8, 29, 2, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    _, target_id = _prepare_terminal(resource_service)
    app = create_app(
        Settings(admin_password="audit-test-password", admin_cookie_secure=False),
        readiness_service=Ready(),  # type: ignore[arg-type]
        execution_resource_service=resource_service,
        execution_scheduling_service=scheduling,
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        ):
            client.headers["x-al1s-csrf"] = "1"
            login = await client.post(
                "/api/v1/auth/login", json={"password": "audit-test-password"}
            )
            assert login.status_code == 200
            created = await client.post(
                "/api/v1/tasks",
                json={
                    "idempotency_key": "http-loop-key",
                    "name": "HTTP loop",
                    "task_type": "loop",
                    "source_module": "test",
                    "logical_content_id": "strategy-1",
                    "requested_target_device_id": str(target_id),
                    "loop": {"repeat_count": 2},
                },
            )
            assert created.status_code == 201
            body = created.json()
            assert body["occurrence_count"] == 2
            schedule_id = body["schedule_id"]

            old_route = await client.post(
                f"/api/v1/tasks/schedules/{schedule_id}/pause",
                json={"expected_version": 1},
            )
            assert old_route.status_code == 404
            paused = await client.post(
                f"/api/v1/task-schedules/{schedule_id}/pause",
                json={"expected_version": 1},
            )
            assert paused.status_code == 200
            assert paused.json()["status"] == "paused"
            active = await client.get("/api/v1/task-schedules?sort=desc")
            assert active.status_code == 200
            active_item = active.json()["items"][0]
            assert active_item["schedule_id"] == schedule_id
            assert active_item["settled_occurrences"] == 0
            assert active_item["current_occurrence_ordinal"] is None
            assert active_item["next_occurrence_ordinal"] == 1
            assert active_item["resume"]["allowed"] is True
            assert active_item["pause"]["allowed"] is False
            history = await client.get("/api/v1/tasks")
            assert history.status_code == 200
            assert history.json()["items"] == []

    asyncio.run(exercise())


def test_schedule_revision_and_original_snapshot_retry_http_contract(engine: Engine) -> None:
    class Ready:
        def check(self) -> ReadinessResult:
            return ReadinessResult(ready=True, dependencies={})

        def close(self) -> None:
            return

    clock = [datetime(2026, 8, 29, 2, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    _, target_id = _prepare_terminal(resource_service)
    timed = scheduling.create_task(
        _timed_command(target_id, key="http-timed-revision"), correlation_id=uuid4()
    )
    assert timed.schedule is not None
    scheduling.create_task(
        _single_command(target_id, key="http-retry-source"), correlation_id=uuid4()
    )
    source = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
    scheduling.start_attempt(source.attempt.attempt_id, correlation_id=uuid4())
    scheduling.complete_attempt(
        source.attempt.attempt_id,
        result=ExecutionResult.FAILURE,
        error_code="assertion_failed",
        retryable=False,
        correlation_id=uuid4(),
    )
    app = create_app(
        Settings(admin_password="audit-test-password", admin_cookie_secure=False),
        readiness_service=Ready(),  # type: ignore[arg-type]
        execution_resource_service=resource_service,
        execution_scheduling_service=scheduling,
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        ):
            client.headers["x-al1s-csrf"] = "1"
            login = await client.post(
                "/api/v1/auth/login", json={"password": "audit-test-password"}
            )
            assert login.status_code == 200
            revised = await client.post(
                f"/api/v1/task-schedules/{timed.schedule.schedule_id}/revision",
                json={
                    "expected_version": timed.schedule.row_version,
                    "start_date": "2026-08-29",
                    "end_date": "2026-08-29",
                    "daily_times": ["12:00:00", "18:00:00"],
                },
            )
            assert revised.status_code == 200
            revision_body = revised.json()
            assert revision_body["revision"] == 2
            assert revision_body["retained_occurrences"] == 1
            assert revision_body["cancelled_occurrences"] == 1
            assert revision_body["added_occurrences"] == 1
            assert revision_body["schedule"]["current_revision"] == 2

            current_occurrences = await client.get(
                f"/api/v1/task-schedules/{timed.schedule.schedule_id}/occurrences",
                params={"scope": "current"},
            )
            assert current_occurrences.status_code == 200
            assert [item["display_ordinal"] for item in current_occurrences.json()["items"]] == [
                1,
                2,
            ]
            assert [item["history_ordinal"] for item in current_occurrences.json()["items"]] == [
                1,
                3,
            ]

            historical_occurrences = await client.get(
                f"/api/v1/task-schedules/{timed.schedule.schedule_id}/occurrences",
                params={"scope": "history"},
            )
            assert historical_occurrences.status_code == 200
            assert historical_occurrences.json()["items"] == [
                {
                    "occurrence_id": str(timed.occurrences[1].occurrence_id),
                    "schedule_revision_id": str(timed.occurrences[1].schedule_revision_id),
                    "schedule_revision": 1,
                    "display_ordinal": None,
                    "history_ordinal": 2,
                    "scheduled_for": "2026-08-29T07:00:00Z",
                    "status": "cancelled",
                    "execution_id": None,
                    "execution_status": None,
                    "execution_result": None,
                }
            ]

            retried = await client.post(
                f"/api/v1/executions/{source.execution.execution_id}/retry-original-snapshot",
                json={"idempotency_key": "http-original-snapshot-retry"},
            )
            assert retried.status_code == 200
            retry_body = retried.json()
            assert retry_body["source_execution_id"] == str(source.execution.execution_id)
            assert retry_body["source_snapshot_id"] == str(source.snapshot.snapshot_id)
            assert retry_body["execution_id"] != str(source.execution.execution_id)

    asyncio.run(exercise())


def test_occurrence_page_and_execution_details_are_bounded_read_models(engine: Engine) -> None:
    class Ready:
        def check(self) -> ReadinessResult:
            return ReadinessResult(ready=True, dependencies={})

        def close(self) -> None:
            return

    clock = [datetime(2026, 8, 29, 2, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    _, target_id = _prepare_terminal(resource_service)
    created = scheduling.create_task(
        _loop_command(target_id, key="read-model-key"), correlation_id=uuid4()
    )
    assert created.schedule is not None
    materialized = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]

    query_count = 0

    def count_query(*_: object) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        details = scheduling.get_execution_details(materialized.execution.execution_id)
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    assert details.snapshot.manifest_hash == materialized.snapshot.manifest_hash
    assert [item.attempt_no for item in details.attempts] == [1]
    assert query_count == 4

    app = create_app(
        Settings(admin_password="audit-test-password", admin_cookie_secure=False),
        readiness_service=Ready(),  # type: ignore[arg-type]
        execution_resource_service=resource_service,
        execution_scheduling_service=scheduling,
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        ):
            client.headers["x-al1s-csrf"] = "1"
            login = await client.post(
                "/api/v1/auth/login", json={"password": "audit-test-password"}
            )
            assert login.status_code == 200
            first_page = await client.get(
                f"/api/v1/task-schedules/{created.schedule.schedule_id}/occurrences?limit=1"
            )
            assert first_page.status_code == 200
            first_body = first_page.json()
            assert len(first_body["items"]) == 1
            assert first_body["items"][0]["execution_id"] == str(
                materialized.execution.execution_id
            )
            assert first_body["items"][0]["display_ordinal"] == 1
            assert first_body["items"][0]["history_ordinal"] == 1
            assert first_body["next_cursor"] is not None
            second_page = await client.get(
                f"/api/v1/task-schedules/{created.schedule.schedule_id}/occurrences",
                params={"cursor": first_body["next_cursor"], "limit": 1},
            )
            assert second_page.status_code == 200
            assert second_page.json()["items"][0]["display_ordinal"] == 2
            assert second_page.json()["items"][0]["history_ordinal"] == 2

            execution = await client.get(
                f"/api/v1/executions/{materialized.execution.execution_id}"
            )
            assert execution.status_code == 200
            execution_body = execution.json()
            assert execution_body["snapshot"]["revision_id"] == "revision-1"
            assert "manifest" not in execution_body["snapshot"]
            assert execution_body["attempts"][0]["attempt_no"] == 1

    asyncio.run(exercise())


def test_single_task_cancel_and_history_delete_preserve_state_boundaries(engine: Engine) -> None:
    clock = [datetime(2026, 8, 29, 2, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    _, target_id = _prepare_terminal(resource_service)

    unstarted = scheduling.create_task(
        _single_command(target_id, key="cancel-unstarted"), correlation_id=uuid4()
    )
    cancelled = scheduling.cancel_single_task(
        unstarted.task.task_id,
        expected_version=unstarted.task.row_version,
        correlation_id=uuid4(),
    )
    assert cancelled.pending_terminal_ack is False
    assert cancelled.execution is None
    assert cancelled.task.lifecycle_status is TaskLifecycleStatus.CANCELLED
    scheduling.delete_task_history(
        cancelled.task.task_id,
        expected_version=cancelled.task.row_version,
        correlation_id=uuid4(),
    )
    with pytest.raises(NotFoundError, match="Task not found"):
        scheduling.get_task(unstarted.task.task_id)

    queued_task = scheduling.create_task(
        _single_command(target_id, key="cancel-queued"), correlation_id=uuid4()
    )
    queued = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
    queued_cancel = scheduling.cancel_single_task(
        queued_task.task.task_id,
        expected_version=queued_task.task.row_version,
        correlation_id=uuid4(),
    )
    assert queued_cancel.pending_terminal_ack is False
    assert queued_cancel.execution is not None
    assert queued_cancel.execution.status.value == "cancelled"
    details = scheduling.get_execution_details(queued.execution.execution_id)
    assert details.attempts[0].status.value == "cancelled"

    running_task = scheduling.create_task(
        _single_command(target_id, key="cancel-running"), correlation_id=uuid4()
    )
    running = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
    scheduling.start_attempt(running.attempt.attempt_id, correlation_id=uuid4())
    requested = scheduling.cancel_single_task(
        running_task.task.task_id,
        expected_version=running_task.task.row_version,
        correlation_id=uuid4(),
    )
    assert requested.pending_terminal_ack is True
    assert requested.execution is not None
    assert requested.execution.status.value == "running"
    assert requested.execution.cancel_requested_at == clock[0]
    replay = scheduling.cancel_single_task(
        running_task.task.task_id,
        expected_version=running_task.task.row_version,
        correlation_id=uuid4(),
    )
    assert replay.execution == requested.execution
    with pytest.raises(ConflictError, match="Active tasks"):
        scheduling.delete_task_history(
            running_task.task.task_id,
            expected_version=running_task.task.row_version,
            correlation_id=uuid4(),
        )

    active_loop = scheduling.create_task(
        _loop_command(target_id, key="cancel-loop"), correlation_id=uuid4()
    )
    with pytest.raises(ConflictError, match="must be terminated"):
        scheduling.cancel_single_task(
            active_loop.task.task_id,
            expected_version=active_loop.task.row_version,
            correlation_id=uuid4(),
        )

    with Session(engine) as session:
        deleted_row = session.get(TaskRequestRow, unstarted.task.task_id)
        assert deleted_row is not None
        assert deleted_row.deleted_at == clock[0]


def test_single_task_cancel_and_history_delete_http_contract(engine: Engine) -> None:
    class Ready:
        def check(self) -> ReadinessResult:
            return ReadinessResult(ready=True, dependencies={})

        def close(self) -> None:
            return

    clock = [datetime(2026, 8, 29, 2, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    _, target_id = _prepare_terminal(resource_service)
    app = create_app(
        Settings(admin_password="audit-test-password", admin_cookie_secure=False),
        readiness_service=Ready(),  # type: ignore[arg-type]
        execution_resource_service=resource_service,
        execution_scheduling_service=scheduling,
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        ):
            client.headers["x-al1s-csrf"] = "1"
            login = await client.post(
                "/api/v1/auth/login", json={"password": "audit-test-password"}
            )
            assert login.status_code == 200
            created = await client.post(
                "/api/v1/tasks",
                json={
                    "idempotency_key": "http-single-cancel",
                    "name": "HTTP single",
                    "task_type": "single",
                    "source_module": "test",
                    "logical_content_id": "strategy-1",
                    "requested_target_device_id": str(target_id),
                },
            )
            assert created.status_code == 201
            task = created.json()
            assert task["row_version"] == 1

            cancelled = await client.post(
                f"/api/v1/tasks/{task['task_id']}/cancel",
                json={"expected_version": task["row_version"]},
            )
            assert cancelled.status_code == 200
            cancelled_body = cancelled.json()
            assert cancelled_body["lifecycle_status"] == "cancelled"
            assert cancelled_body["row_version"] == 2

            history = await client.get("/api/v1/tasks")
            assert history.status_code == 200
            assert history.json()["items"][0]["row_version"] == 2

            deleted = await client.request(
                "DELETE",
                f"/api/v1/tasks/{task['task_id']}",
                json={"expected_version": cancelled_body["row_version"]},
            )
            assert deleted.status_code == 204
            missing = await client.get(f"/api/v1/tasks/{task['task_id']}")
            assert missing.status_code == 404
            empty_history = await client.get("/api/v1/tasks")
            assert empty_history.json()["items"] == []

    asyncio.run(exercise())


