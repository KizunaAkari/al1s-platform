from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.delivery_models import (
    CommandAcknowledgementRow,
    OfflineStartPermitRow,
    TaskPackageRow,
    TerminalCommandRow,
    TerminalReportRow,
)
from al1s.adapters.postgres.execution_models import ExecutionLeaseRow
from al1s.adapters.postgres.models import OutboxEventRow
from al1s.app.config import Settings
from al1s.app.factory import create_app
from al1s.execution.delivery_types import (
    CancellationAcknowledgementOutcome,
    CommandKind,
    PackageReceiptDisposition,
    PackageStatus,
    RejectionDisposition,
    TerminalReportDisposition,
    TerminalResultKind,
)
from al1s.execution.scheduling_types import (
    ExecutionResult,
    FailurePhase,
    TaskLifecycleStatus,
)
from al1s.infrastructure.readiness import ReadinessResult
from tests.integration.execution_support import (
    _delivery_services,
    _prepare_terminal,
    _prepare_terminal_with_credential,
    _services,
    _single_command,
)
from tests.integration.execution_support import (
    clean_tables as clean_tables,
)
from tests.integration.execution_support import (
    engine as engine,
)

pytestmark = pytest.mark.integration


def test_terminal_delivery_happy_path_is_idempotent_and_fenced(engine: Engine) -> None:
    clock = [datetime(2026, 8, 29, 3, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    delivery, _ = _delivery_services(engine, clock)
    terminal_id, target_id = _prepare_terminal(resource_service)
    task = scheduling.create_task(
        _single_command(target_id, key="delivery-happy"), correlation_id=uuid4()
    )
    materialized = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]

    assert materialized.execution.status.value == "waiting"
    commands = delivery.list_commands(terminal_id)
    assert len(commands) == 1
    command = commands[0]
    assert command.command_kind is CommandKind.TASK_PACKAGE_AVAILABLE
    assert command.package_id is not None

    receipt_id = uuid4()
    receipt = delivery.receive_package(
        terminal_id=terminal_id,
        package_id=command.package_id,
        attempt_id=command.attempt_id,
        report_id=receipt_id,
        command_id=command.command_id,
        disposition=PackageReceiptDisposition.ACCEPTED,
        rejection_code=None,
        diagnostic=None,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert receipt.package.status is PackageStatus.ACCEPTED
    assert receipt.execution.status.value == "queued"
    assert receipt.offline_start_permit is not None
    replay = delivery.receive_package(
        terminal_id=terminal_id,
        package_id=command.package_id,
        attempt_id=command.attempt_id,
        report_id=receipt_id,
        command_id=command.command_id,
        disposition=PackageReceiptDisposition.ACCEPTED,
        rejection_code=None,
        diagnostic=None,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert replay.report == receipt.report

    start_id = uuid4()
    started = delivery.start_attempt(
        terminal_id=terminal_id,
        attempt_id=command.attempt_id,
        package_id=command.package_id,
        offline_permit_id=receipt.offline_start_permit.permit_id,
        offline_permit_token=receipt.offline_start_permit.token,
        report_id=start_id,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert started.execution.status.value == "running"
    assert started.attempt.status.value == "running"
    start_replay = delivery.start_attempt(
        terminal_id=terminal_id,
        attempt_id=command.attempt_id,
        package_id=command.package_id,
        offline_permit_id=receipt.offline_start_permit.permit_id,
        offline_permit_token=receipt.offline_start_permit.token,
        report_id=start_id,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert start_replay.lease == started.lease

    result_id = uuid4()
    diagnostic = {
        "modules": [
            {
                "result": {
                    "conditional_skips": [
                        {"module_number": 3, "module_step_number": 2, "trigger_step_number": 54}
                    ]
                }
            }
        ]
    }
    result = delivery.receive_result(
        diagnostic=diagnostic,
        terminal_id=terminal_id,
        attempt_id=command.attempt_id,
        package_id=command.package_id,
        report_id=result_id,
        lease_id=started.lease.lease_id,
        lease_version=started.lease.row_version,
        result_kind=TerminalResultKind.SUCCESS,
        error_code=None,
        retryable=False,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert result.report.disposition is TerminalReportDisposition.ACCEPTED
    assert result.execution.status.value == "ended"
    assert result.attempt.result is ExecutionResult.SUCCESS
    assert result.retry_attempt is None
    result_replay = delivery.receive_result(
        diagnostic=diagnostic,
        terminal_id=terminal_id,
        attempt_id=command.attempt_id,
        package_id=command.package_id,
        report_id=result_id,
        lease_id=started.lease.lease_id,
        lease_version=started.lease.row_version,
        result_kind=TerminalResultKind.SUCCESS,
        error_code=None,
        retryable=False,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert result_replay.report == result.report
    assert result_replay.report.result_diagnostic == diagnostic
    with Session(engine) as session:
        skip_events = session.scalars(
            select(OutboxEventRow).where(
                OutboxEventRow.event_type == "execution.conditional_skip.v1"
            )
        ).all()
        assert len(skip_events) == 1
        assert skip_events[0].payload["module_number"] == 3
        assert skip_events[0].payload["module_step_number"] == 2
    assert (
        scheduling.get_task(task.task.task_id).task.lifecycle_status
        is TaskLifecycleStatus.COMPLETED
    )

    with Session(engine) as session:
        lease = session.get(ExecutionLeaseRow, started.lease.lease_id)
        assert lease is not None and lease.released_at == clock[0]
        assert session.scalar(select(func.count()).select_from(TerminalReportRow)) == 3


def test_offline_cancellation_reconciles_before_during_and_after_execution(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 9, 1, 1, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    delivery, _ = _delivery_services(engine, clock)
    terminal_id, target_id = _prepare_terminal(resource_service)

    def accept_package(key: str):  # type: ignore[no-untyped-def]
        task = scheduling.create_task(_single_command(target_id, key=key), correlation_id=uuid4())
        materialized = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
        package_command = delivery.list_commands(terminal_id)[0]
        assert package_command.package_id is not None
        receipt = delivery.receive_package(
            terminal_id=terminal_id,
            package_id=package_command.package_id,
            attempt_id=package_command.attempt_id,
            report_id=uuid4(),
            command_id=package_command.command_id,
            disposition=PackageReceiptDisposition.ACCEPTED,
            rejection_code=None,
            diagnostic=None,
            occurred_at=clock[0],
            correlation_id=uuid4(),
        )
        assert receipt.offline_start_permit is not None
        return task, materialized, package_command, receipt.offline_start_permit

    task, materialized, package_command, permit = accept_package("cancel-before-start")
    requested = scheduling.cancel_single_task(
        task.task.task_id,
        expected_version=task.task.row_version,
        correlation_id=uuid4(),
    )
    assert requested.execution is not None
    assert requested.execution.status.value == "queued"
    assert requested.execution.cancel_deadline_at is None
    cancel_command = delivery.list_commands(terminal_id)[0]
    report_id = uuid4()
    before_start = delivery.acknowledge_cancel_command(
        terminal_id=terminal_id,
        command_id=cancel_command.command_id,
        report_id=report_id,
        outcome=CancellationAcknowledgementOutcome.CANCELLED_BEFORE_START,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    replay = delivery.acknowledge_cancel_command(
        terminal_id=terminal_id,
        command_id=cancel_command.command_id,
        report_id=report_id,
        outcome=CancellationAcknowledgementOutcome.CANCELLED_BEFORE_START,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert replay.acknowledgement == before_start.acknowledgement
    assert before_start.execution.status.value == "cancelled"
    assert (
        scheduling.get_task(task.task.task_id).task.lifecycle_status
        is TaskLifecycleStatus.CANCELLED
    )
    with Session(engine) as session:
        package_row = session.get(TaskPackageRow, package_command.package_id)
        permit_row = session.get(OfflineStartPermitRow, permit.permit_id)
        assert package_row is not None and package_row.status == "cancelled"
        assert permit_row is not None and permit_row.status == "revoked"

    task, materialized, package_command, permit = accept_package("cancel-while-running")
    scheduling.cancel_single_task(
        task.task.task_id,
        expected_version=task.task.row_version,
        correlation_id=uuid4(),
    )
    assert package_command.package_id is not None
    started = delivery.start_attempt(
        terminal_id=terminal_id,
        attempt_id=materialized.attempt.attempt_id,
        package_id=package_command.package_id,
        offline_permit_id=permit.permit_id,
        offline_permit_token=permit.token,
        report_id=uuid4(),
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    cancel_command = delivery.list_commands(terminal_id)[0]
    running_ack = delivery.acknowledge_cancel_command(
        terminal_id=terminal_id,
        command_id=cancel_command.command_id,
        report_id=uuid4(),
        outcome=CancellationAcknowledgementOutcome.RUNNING_CANCEL_ACCEPTED,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert running_ack.execution.cancel_deadline_at == clock[0] + timedelta(seconds=60)
    cancelled_result = delivery.receive_result(
        terminal_id=terminal_id,
        attempt_id=materialized.attempt.attempt_id,
        package_id=package_command.package_id,
        report_id=uuid4(),
        lease_id=started.lease.lease_id,
        lease_version=started.lease.row_version,
        result_kind=TerminalResultKind.CANCELLED,
        error_code="operator_cancelled",
        retryable=False,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert cancelled_result.execution.status.value == "cancelled"
    assert (
        scheduling.get_task(task.task.task_id).task.lifecycle_status
        is TaskLifecycleStatus.CANCELLED
    )

    task, materialized, package_command, permit = accept_package("cancel-after-complete")
    scheduling.cancel_single_task(
        task.task.task_id,
        expected_version=task.task.row_version,
        correlation_id=uuid4(),
    )
    assert package_command.package_id is not None
    started = delivery.start_attempt(
        terminal_id=terminal_id,
        attempt_id=materialized.attempt.attempt_id,
        package_id=package_command.package_id,
        offline_permit_id=permit.permit_id,
        offline_permit_token=permit.token,
        report_id=uuid4(),
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    completed = delivery.receive_result(
        terminal_id=terminal_id,
        attempt_id=materialized.attempt.attempt_id,
        package_id=package_command.package_id,
        report_id=uuid4(),
        lease_id=started.lease.lease_id,
        lease_version=started.lease.row_version,
        result_kind=TerminalResultKind.SUCCESS,
        error_code=None,
        retryable=False,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert completed.execution.status.value == "ended"
    assert completed.execution.result is ExecutionResult.SUCCESS
    assert completed.execution.cancel_delivery_delayed is True
    assert completed.execution.cancel_delivery_note == "取消请求未能及时送达"
    assert (
        scheduling.get_task(task.task.task_id).task.lifecycle_status
        is TaskLifecycleStatus.COMPLETED
    )

    task, materialized, package_command, permit = accept_package("cancel-completed-race")
    scheduling.cancel_single_task(
        task.task.task_id,
        expected_version=task.task.row_version,
        correlation_id=uuid4(),
    )
    assert package_command.package_id is not None
    started = delivery.start_attempt(
        terminal_id=terminal_id,
        attempt_id=materialized.attempt.attempt_id,
        package_id=package_command.package_id,
        offline_permit_id=permit.permit_id,
        offline_permit_token=permit.token,
        report_id=uuid4(),
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    cancel_command = delivery.list_commands(terminal_id)[0]
    completed_ack = delivery.acknowledge_cancel_command(
        terminal_id=terminal_id,
        command_id=cancel_command.command_id,
        report_id=uuid4(),
        outcome=CancellationAcknowledgementOutcome.ALREADY_COMPLETED,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert completed_ack.execution.status.value == "running"
    assert completed_ack.execution.cancel_delivery_delayed is True
    accepted_result = delivery.receive_result(
        terminal_id=terminal_id,
        attempt_id=materialized.attempt.attempt_id,
        package_id=package_command.package_id,
        report_id=uuid4(),
        lease_id=started.lease.lease_id,
        lease_version=started.lease.row_version,
        result_kind=TerminalResultKind.SUCCESS,
        error_code=None,
        retryable=False,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert accepted_result.report.disposition is TerminalReportDisposition.ACCEPTED
    assert accepted_result.execution.cancel_delivery_delayed is True
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(CommandAcknowledgementRow)) == 3


def test_concurrent_duplicate_package_receipt_mutates_state_once(engine: Engine) -> None:
    clock = [datetime(2026, 8, 29, 3, 30, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    delivery, _ = _delivery_services(engine, clock)
    terminal_id, target_id = _prepare_terminal(resource_service)
    scheduling.create_task(
        _single_command(target_id, key="delivery-concurrent-report"),
        correlation_id=uuid4(),
    )
    scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())
    command = delivery.list_commands(terminal_id)[0]
    assert command.package_id is not None
    report_id = uuid4()

    def acknowledge() -> object:
        return delivery.receive_package(
            terminal_id=terminal_id,
            package_id=command.package_id,
            attempt_id=command.attempt_id,
            report_id=report_id,
            command_id=command.command_id,
            disposition=PackageReceiptDisposition.ACCEPTED,
            rejection_code=None,
            diagnostic=None,
            occurred_at=clock[0],
            correlation_id=uuid4(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(acknowledge) for _ in range(2)]
        first, second = [future.result() for future in futures]

    assert first == second
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TerminalReportRow)) == 1
        package = session.get(TaskPackageRow, command.package_id)
        assert package is not None and package.row_version == 2


def test_terminal_rejection_retains_reason_and_separates_delivery_failure(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 8, 29, 4, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    delivery, _ = _delivery_services(engine, clock)
    terminal_id, target_id = _prepare_terminal(resource_service)

    scheduling.create_task(
        _single_command(target_id, key="delivery-blocked"), correlation_id=uuid4()
    )
    blocked = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
    blocked_command = delivery.list_commands(terminal_id)[0]
    assert blocked_command.package_id is not None
    blocked_report_id = uuid4()
    blocked_receipt = delivery.receive_package(
        terminal_id=terminal_id,
        package_id=blocked_command.package_id,
        attempt_id=blocked.attempt.attempt_id,
        report_id=blocked_report_id,
        command_id=blocked_command.command_id,
        disposition=PackageReceiptDisposition.REJECTED,
        rejection_code="TARGET_DEVICE_UNAVAILABLE",
        diagnostic="Android phone is disconnected from the Linux terminal",
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert blocked_receipt.package.status is PackageStatus.AVAILABLE
    assert blocked_receipt.package.last_rejection_disposition is RejectionDisposition.BLOCKED_WAIT
    assert blocked_receipt.package.last_rejection_code == "TARGET_DEVICE_UNAVAILABLE"
    assert blocked_receipt.execution.status.value == "waiting"

    redelivery = delivery.redeliver_blocked_execution(
        blocked.execution.execution_id,
        expected_version=blocked_receipt.execution.row_version,
        correlation_id=uuid4(),
    )
    assert redelivery.package.package_id == blocked_command.package_id
    assert redelivery.command.delivery_no == 2
    permanent_command = delivery.list_commands(terminal_id)[0]
    assert permanent_command.package_id == blocked_command.package_id
    permanent_receipt = delivery.receive_package(
        terminal_id=terminal_id,
        package_id=permanent_command.package_id,
        attempt_id=blocked.attempt.attempt_id,
        report_id=uuid4(),
        command_id=permanent_command.command_id,
        disposition=PackageReceiptDisposition.REJECTED,
        rejection_code="CAPABILITY_MISMATCH",
        diagnostic="Required Maa provider version is not available",
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert permanent_receipt.package.status is PackageStatus.DELIVERY_FAILED
    assert permanent_receipt.execution.status.value == "ended"
    assert permanent_receipt.execution.result is ExecutionResult.FAILURE
    details = scheduling.get_execution_details(blocked.execution.execution_id)
    assert details.attempts[0].failure_phase is FailurePhase.DELIVERY
    assert details.attempts[0].error_code == "CAPABILITY_MISMATCH"

    with Session(engine) as session:
        blocked_row = session.get(TaskPackageRow, blocked_command.package_id)
        assert blocked_row is not None
        assert blocked_row.last_rejection_diagnostic == (
            "Required Maa provider version is not available"
        )
        blocked_report = session.get(TerminalReportRow, (terminal_id, blocked_report_id))
        assert blocked_report is not None
        assert blocked_report.diagnostic == (
            "Android phone is disconnected from the Linux terminal"
        )


def test_runtime_timeout_releases_lease_and_late_result_is_stale(engine: Engine) -> None:
    clock = [datetime(2026, 8, 29, 5, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    delivery, timeout = _delivery_services(engine, clock)
    terminal_id, target_id = _prepare_terminal(resource_service)
    task = scheduling.create_task(
        _single_command(target_id, key="delivery-timeout"), correlation_id=uuid4()
    )
    materialized = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
    command = delivery.list_commands(terminal_id)[0]
    assert command.package_id is not None
    receipt = delivery.receive_package(
        terminal_id=terminal_id,
        package_id=command.package_id,
        attempt_id=materialized.attempt.attempt_id,
        report_id=uuid4(),
        command_id=command.command_id,
        disposition=PackageReceiptDisposition.ACCEPTED,
        rejection_code=None,
        diagnostic=None,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert receipt.offline_start_permit is not None
    started = delivery.start_attempt(
        terminal_id=terminal_id,
        attempt_id=materialized.attempt.attempt_id,
        package_id=command.package_id,
        offline_permit_id=receipt.offline_start_permit.permit_id,
        offline_permit_token=receipt.offline_start_permit.token,
        report_id=uuid4(),
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )

    clock[0] += timedelta(seconds=301)
    assert timeout.request_timed_out_cancellation() == 1
    cancel_commands = delivery.list_commands(terminal_id)
    assert len(cancel_commands) == 1
    assert cancel_commands[0].command_kind is CommandKind.CANCEL_REQUESTED
    assert cancel_commands[0].payload["cleanup_grace_seconds"] == 60
    assert timeout.request_timed_out_cancellation() == 0

    clock[0] += timedelta(seconds=61)
    assert timeout.force_expired_cleanup() == 1
    timed_out = scheduling.get_execution_details(materialized.execution.execution_id)
    assert timed_out.execution.status.value == "timed_out"
    assert timed_out.attempts[0].status.value == "timed_out"
    assert timed_out.attempts[0].failure_phase is FailurePhase.CLEANUP
    assert (
        scheduling.get_task(task.task.task_id).task.lifecycle_status
        is TaskLifecycleStatus.COMPLETED
    )

    late = delivery.receive_result(
        terminal_id=terminal_id,
        attempt_id=materialized.attempt.attempt_id,
        package_id=command.package_id,
        report_id=uuid4(),
        lease_id=started.lease.lease_id,
        lease_version=started.lease.row_version,
        result_kind=TerminalResultKind.SUCCESS,
        error_code=None,
        retryable=False,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert late.report.disposition is TerminalReportDisposition.STALE
    assert late.execution.status.value == "timed_out"

    with Session(engine) as session:
        lease = session.get(ExecutionLeaseRow, started.lease.lease_id)
        assert lease is not None and lease.released_at is not None
        assert session.scalar(select(func.count()).select_from(TerminalCommandRow)) == 2


def test_timeout_worker_query_cost_is_measured_and_batch_bounded(engine: Engine) -> None:
    clock = [datetime(2026, 8, 29, 5, 30, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    delivery, timeout = _delivery_services(engine, clock)

    for index in range(2):
        terminal_id, target_id = _prepare_terminal(resource_service)
        scheduling.create_task(
            _single_command(target_id, key=f"timeout-query-cost-{index}"),
            correlation_id=uuid4(),
        )
        materialized = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
        command = delivery.list_commands(terminal_id)[0]
        assert command.package_id is not None
        receipt = delivery.receive_package(
            terminal_id=terminal_id,
            package_id=command.package_id,
            attempt_id=materialized.attempt.attempt_id,
            report_id=uuid4(),
            command_id=command.command_id,
            disposition=PackageReceiptDisposition.ACCEPTED,
            rejection_code=None,
            diagnostic=None,
            occurred_at=clock[0],
            correlation_id=uuid4(),
        )
        assert receipt.offline_start_permit is not None
        delivery.start_attempt(
            terminal_id=terminal_id,
            attempt_id=materialized.attempt.attempt_id,
            package_id=command.package_id,
            offline_permit_id=receipt.offline_start_permit.permit_id,
            offline_permit_token=receipt.offline_start_permit.token,
            report_id=uuid4(),
            occurred_at=clock[0],
            correlation_id=uuid4(),
        )

    query_count = 0

    def count_query(*_args: object) -> None:
        nonlocal query_count
        query_count += 1

    clock[0] += timedelta(seconds=301)
    event.listen(engine, "before_cursor_execute", count_query)
    try:
        assert timeout.request_timed_out_cancellation(limit=2) == 2
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    cancellation_queries = query_count

    query_count = 0
    clock[0] += timedelta(seconds=61)
    event.listen(engine, "before_cursor_execute", count_query)
    try:
        assert timeout.force_expired_cleanup(limit=2) == 2
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    cleanup_queries = query_count

    assert cancellation_queries == 9
    assert cleanup_queries == 21


def test_cleanup_deadline_and_terminal_result_converge_without_overwrite(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 8, 29, 5, 45, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    delivery, timeout = _delivery_services(engine, clock)
    terminal_id, target_id = _prepare_terminal(resource_service)
    scheduling.create_task(
        _single_command(target_id, key="cleanup-result-race"), correlation_id=uuid4()
    )
    materialized = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
    command = delivery.list_commands(terminal_id)[0]
    assert command.package_id is not None
    receipt = delivery.receive_package(
        terminal_id=terminal_id,
        package_id=command.package_id,
        attempt_id=materialized.attempt.attempt_id,
        report_id=uuid4(),
        command_id=command.command_id,
        disposition=PackageReceiptDisposition.ACCEPTED,
        rejection_code=None,
        diagnostic=None,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    assert receipt.offline_start_permit is not None
    started = delivery.start_attempt(
        terminal_id=terminal_id,
        attempt_id=materialized.attempt.attempt_id,
        package_id=command.package_id,
        offline_permit_id=receipt.offline_start_permit.permit_id,
        offline_permit_token=receipt.offline_start_permit.token,
        report_id=uuid4(),
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    clock[0] += timedelta(seconds=301)
    assert timeout.request_timed_out_cancellation() == 1
    clock[0] += timedelta(seconds=61)

    barrier = Barrier(2)

    def force_cleanup() -> int:
        barrier.wait()
        return timeout.force_expired_cleanup()

    def return_result() -> TerminalReportDisposition:
        barrier.wait()
        result = delivery.receive_result(
            terminal_id=terminal_id,
            attempt_id=materialized.attempt.attempt_id,
            package_id=command.package_id,
            report_id=uuid4(),
            lease_id=started.lease.lease_id,
            lease_version=started.lease.row_version,
            result_kind=TerminalResultKind.TIMED_OUT,
            error_code=None,
            retryable=False,
            occurred_at=clock[0],
            correlation_id=uuid4(),
        )
        return result.report.disposition

    with ThreadPoolExecutor(max_workers=2) as pool:
        cleanup_future = pool.submit(force_cleanup)
        result_future = pool.submit(return_result)
        cleanup_count = cleanup_future.result(timeout=10)
        report_disposition = result_future.result(timeout=10)

    details = scheduling.get_execution_details(materialized.execution.execution_id)
    assert details.execution.status.value == "timed_out"
    assert details.execution.result is None
    assert details.attempts[0].status.value == "timed_out"
    assert cleanup_count in {0, 1}
    assert report_disposition in {
        TerminalReportDisposition.ACCEPTED,
        TerminalReportDisposition.STALE,
    }
    assert cleanup_count == 0 or report_disposition is TerminalReportDisposition.STALE


def test_cancel_command_acknowledgement_http_contract(engine: Engine) -> None:
    class Ready:
        def check(self) -> ReadinessResult:
            return ReadinessResult(ready=True, dependencies={})

        def close(self) -> None:
            return

    clock = [datetime(2026, 9, 1, 2, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    delivery, _ = _delivery_services(engine, clock)
    terminal_id, target_id, credential = _prepare_terminal_with_credential(resource_service)
    task = scheduling.create_task(
        _single_command(target_id, key="cancel-ack-http"), correlation_id=uuid4()
    )
    materialized = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
    package_command = delivery.list_commands(terminal_id)[0]
    assert package_command.package_id is not None
    delivery.receive_package(
        terminal_id=terminal_id,
        package_id=package_command.package_id,
        attempt_id=materialized.attempt.attempt_id,
        report_id=uuid4(),
        command_id=package_command.command_id,
        disposition=PackageReceiptDisposition.ACCEPTED,
        rejection_code=None,
        diagnostic=None,
        occurred_at=clock[0],
        correlation_id=uuid4(),
    )
    scheduling.cancel_single_task(
        task.task.task_id,
        expected_version=task.task.row_version,
        correlation_id=uuid4(),
    )
    cancel_command = delivery.list_commands(terminal_id)[0]
    app = create_app(
        Settings(),
        readiness_service=Ready(),  # type: ignore[arg-type]
        execution_resource_service=resource_service,
        execution_scheduling_service=scheduling,
        terminal_delivery_service=delivery,
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        payload = {
            "report_id": str(uuid4()),
            "outcome": "cancelled_before_start",
            "occurred_at": clock[0].isoformat(),
        }
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        ):
            unauthorized = await client.post(
                f"/api/v1/terminal/commands/{cancel_command.command_id}/acknowledgement",
                json=payload,
            )
            assert unauthorized.status_code == 401
            response = await client.post(
                f"/api/v1/terminal/commands/{cancel_command.command_id}/acknowledgement",
                headers={"Authorization": f"Bearer {credential}"},
                json=payload,
            )
            assert response.status_code == 200
            assert response.json() == {
                "report_id": payload["report_id"],
                "disposition": "accepted",
                "command_status": "acknowledged",
                "execution_status": "cancelled",
            }

    asyncio.run(exercise())


def test_terminal_http_contract_authenticates_and_executes_package(engine: Engine) -> None:
    class Ready:
        def check(self) -> ReadinessResult:
            return ReadinessResult(ready=True, dependencies={})

        def close(self) -> None:
            return

    clock = [datetime(2026, 8, 29, 6, 0, tzinfo=UTC)]
    resource_service, scheduling = _services(engine, clock)
    delivery, _ = _delivery_services(engine, clock)
    terminal_id, target_id, credential = _prepare_terminal_with_credential(resource_service)
    scheduling.create_task(_single_command(target_id, key="terminal-http"), correlation_id=uuid4())
    scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())
    app = create_app(
        Settings(),
        readiness_service=Ready(),  # type: ignore[arg-type]
        execution_resource_service=resource_service,
        execution_scheduling_service=scheduling,
        terminal_delivery_service=delivery,
    )

    async def exercise() -> None:
        headers = {"Authorization": f"Bearer {credential}"}
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        ):
            unauthorized = await client.get("/api/v1/terminal/commands")
            assert unauthorized.status_code == 401

            listed = await client.get("/api/v1/terminal/commands", headers=headers)
            assert listed.status_code == 200
            command = listed.json()["items"][0]
            package_id = command["package_id"]
            attempt_id = command["attempt_id"]

            package = await client.get(
                f"/api/v1/terminal/task-packages/{package_id}", headers=headers
            )
            assert package.status_code == 200
            assert package.json()["status"] == "available"
            assert package.json()["body"]["terminal_id"] == str(terminal_id)

            receipt = await client.post(
                f"/api/v1/terminal/task-packages/{package_id}/receipt",
                headers=headers,
                json={
                    "report_id": str(uuid4()),
                    "command_id": command["command_id"],
                    "attempt_id": attempt_id,
                    "disposition": "accepted",
                    "occurred_at": clock[0].isoformat(),
                },
            )
            assert receipt.status_code == 200
            receipt_body = receipt.json()
            assert receipt_body["execution_status"] == "queued"
            permit = receipt_body["offline_start_permit"]

            started = await client.post(
                f"/api/v1/terminal/attempts/{attempt_id}/start",
                headers=headers,
                json={
                    "report_id": str(uuid4()),
                    "package_id": package_id,
                    "offline_permit_id": permit["permit_id"],
                    "offline_permit_token": permit["token"],
                    "occurred_at": clock[0].isoformat(),
                },
            )
            assert started.status_code == 200
            started_body = started.json()
            assert started_body["execution_status"] == "running"

            result = await client.post(
                f"/api/v1/terminal/attempts/{attempt_id}/result",
                headers=headers,
                json={
                    "report_id": str(uuid4()),
                    "package_id": package_id,
                    "lease_id": started_body["lease_id"],
                    "lease_version": started_body["lease_version"],
                    "result": "success",
                    "occurred_at": clock[0].isoformat(),
                },
            )
            assert result.status_code == 200
            assert result.json()["execution_status"] == "ended"

    asyncio.run(exercise())
