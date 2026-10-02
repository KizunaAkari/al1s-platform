"""Expired Android permits reconcile with operator cancellation under one lock order."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.delivery_models import TerminalReportRow
from al1s.execution.delivery_service import TerminalDeliveryService
from al1s.execution.delivery_types import (
    PackageReceiptDisposition,
    PrestartFailureResult,
    TerminalReportDisposition,
)
from al1s.execution.errors import ConflictError
from al1s.execution.scheduling_service import ExecutionSchedulingService
from al1s.execution.scheduling_types import TaskCancellation, TaskLifecycleStatus
from tests.integration.execution_support import (
    _delivery_services,
    _prepare_terminal,
    _services,
    _single_command,
)
from tests.integration.execution_support import clean_tables as clean_tables
from tests.integration.execution_support import engine as engine

pytestmark = pytest.mark.integration


@dataclass
class Case:
    scheduling: ExecutionSchedulingService
    delivery: TerminalDeliveryService
    clock: list[datetime]
    terminal_id: UUID
    task_id: UUID
    package_id: UUID
    attempt_id: UUID
    permit_id: UUID
    task_version: int

    def cancel(self) -> TaskCancellation:
        return self.scheduling.cancel_single_task(
            self.task_id, expected_version=self.task_version, correlation_id=uuid4()
        )

    def report(self, report_id: UUID) -> PrestartFailureResult:
        return self.delivery.settle_expired_prestart(
            terminal_id=self.terminal_id,
            attempt_id=self.attempt_id,
            package_id=self.package_id,
            permit_id=self.permit_id,
            report_id=report_id,
            occurred_at=self.clock[0],
            correlation_id=uuid4(),
        )


def _accepted(engine: Engine) -> Case:
    clock = [datetime(2026, 9, 28, tzinfo=UTC)]
    resources, scheduling = _services(engine, clock)
    delivery, _ = _delivery_services(engine, clock)
    terminal_id, target_id = _prepare_terminal(resources)
    task = scheduling.create_task(
        _single_command(target_id, key=f"prestart-{uuid4()}"), correlation_id=uuid4()
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
    clock[0] = receipt.offline_start_permit.expires_at + timedelta(seconds=1)
    return Case(
        scheduling, delivery, clock, terminal_id, task.task.task_id,
        command.package_id, materialized.attempt.attempt_id,
        receipt.offline_start_permit.permit_id, task.task.row_version,
    )


def _report_count(engine: Engine, report_id: UUID) -> int:
    with Session(engine) as session:
        return session.scalar(
            select(func.count())
            .select_from(TerminalReportRow)
            .where(TerminalReportRow.report_id == report_id)
        ) or 0


def test_platform_cancellation_then_expired_report_replays_one_confirmation(engine: Engine) -> None:
    case = _accepted(engine)
    cancelled = case.cancel()
    assert cancelled.execution is not None and cancelled.execution.status.value == "cancelled"
    report_id = uuid4()
    first = case.report(report_id)
    second = case.report(report_id)
    assert first.report.disposition is TerminalReportDisposition.STALE
    assert second.report == first.report
    assert first.attempt.status.value == first.execution.status.value == "cancelled"
    assert _report_count(engine, report_id) == 1


def test_expired_report_then_operator_cancel_keeps_original_settlement(engine: Engine) -> None:
    case = _accepted(engine)
    report_id = uuid4()
    first = case.report(report_id)
    assert first.report.disposition is TerminalReportDisposition.ACCEPTED
    with pytest.raises(ConflictError):
        case.cancel()
    assert case.report(report_id).report == first.report
    task = case.scheduling.get_task(case.task_id).task
    assert task.lifecycle_status is TaskLifecycleStatus.COMPLETED


def test_cancelled_history_deletion_does_not_break_late_report(engine: Engine) -> None:
    case = _accepted(engine)
    cancelled = case.cancel()
    case.scheduling.delete_task_history(
        case.task_id, expected_version=cancelled.task.row_version, correlation_id=uuid4()
    )
    report_id = uuid4()
    first = case.report(report_id)
    assert first.report.disposition is TerminalReportDisposition.STALE
    assert case.report(report_id).report == first.report
    assert _report_count(engine, report_id) == 1


def test_parallel_cancel_and_expired_report_have_one_terminal_outcome(engine: Engine) -> None:
    case = _accepted(engine)
    barrier = Barrier(2)
    report_id = uuid4()

    def cancel() -> str:
        barrier.wait()
        try:
            case.cancel()
            return "cancelled"
        except ConflictError:
            return "already_settled"

    def report() -> TerminalReportDisposition:
        barrier.wait()
        return case.report(report_id).report.disposition

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel_future = pool.submit(cancel)
        report_future = pool.submit(report)
        cancel_outcome = cancel_future.result(timeout=10)
        disposition = report_future.result(timeout=10)

    assert (cancel_outcome, disposition) in {
        ("cancelled", TerminalReportDisposition.STALE),
        ("already_settled", TerminalReportDisposition.ACCEPTED),
    }
    assert _report_count(engine, report_id) == 1
