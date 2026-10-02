"""Settle an accepted task whose offline start permit expired before execution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from al1s.execution.delivery_failure import settle_delivery_failure
from al1s.execution.delivery_support import payload_hash, record_delivery_event, terminal_transition
from al1s.execution.delivery_types import (
    PackageStatus,
    PrestartFailureResult,
    TaskPackageRecord,
    TerminalReportDisposition,
    TerminalReportKind,
    TerminalReportRecord,
)
from al1s.execution.errors import ConflictError, NotFoundError
from al1s.execution.offline_permits import OfflinePermitStatus, OfflineStartPermitRecord
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_types import (
    AttemptStatus,
    ExecutionAttemptRecord,
    ExecutionRecord,
    ExecutionStatus,
    TaskLifecycleStatus,
)

ERROR_CODE = "offline_permit_expired"
CANCELLED_CODE = "task_cancelled"


class PrestartFailureMixin:
    _uow_factory: Callable[[], SchedulingUnitOfWork]
    _now: Callable[[], datetime]

    def settle_expired_prestart(
        self,
        *,
        terminal_id: UUID,
        attempt_id: UUID,
        package_id: UUID,
        permit_id: UUID,
        report_id: UUID,
        occurred_at: datetime,
        correlation_id: UUID,
    ) -> PrestartFailureResult:
        expected_hash = payload_hash(
            {
                "attempt_id": str(attempt_id),
                "package_id": str(package_id),
                "permit_id": str(permit_id),
                "reason": ERROR_CODE,
            }
        )
        now = self._now()
        with self._uow_factory() as uow:
            uow.terminal_reports.lock_idempotency_key(terminal_id, report_id)
            existing = uow.terminal_reports.get(terminal_id, report_id)
            route = uow.packages.execution_route_for_package(package_id)
            if route is None:
                raise NotFoundError("execution_attempt")
            task_id, execution_id = route
            task = uow.tasks.get_for_reconciliation(task_id)
            execution = uow.executions.get_for_update(execution_id)
            attempt = uow.attempts.get_for_update(attempt_id)
            package = uow.packages.get_for_update(package_id)
            if (
                task is None
                or execution is None
                or execution.task_request_id != task_id
                or execution.terminal_id != terminal_id
                or attempt is None
                or attempt.execution_id != execution_id
                or package is None
                or package.execution_id != execution_id
                or package.terminal_id != terminal_id
                or package.attempt_id != attempt_id
            ):
                raise NotFoundError("execution_attempt")
            if existing is not None:
                if (
                    existing.report_kind is not TerminalReportKind.ATTEMPT_RESULT
                    or existing.package_id != package_id
                    or existing.attempt_id != attempt_id
                    or existing.lease_id is not None
                    or existing.payload_hash != expected_hash
                ):
                    raise ConflictError(
                        "terminal_report_reused", "Report ID belongs to another result"
                    )
                return PrestartFailureResult(execution, attempt, existing)

            permit = uow.offline_permits.get_by_attempt_for_update(attempt_id)
            if (
                permit is None
                or permit.permit_id != permit_id
                or permit.terminal_id != terminal_id
                or permit.package_id != package_id
                or permit.execution_id != execution.execution_id
                or permit.package_hash != package.package_hash
            ):
                raise ConflictError("offline_permit_binding_mismatch", "Offline permit is invalid")
            if now < permit.expires_at:
                raise ConflictError("offline_permit_not_expired", "Offline permit is still valid")
            if (
                package.status is PackageStatus.ACCEPTED
                and attempt.status is AttemptStatus.CANCELLED
                and execution.status is ExecutionStatus.CANCELLED
                and task.lifecycle_status in {
                    TaskLifecycleStatus.CANCELLED,
                    TaskLifecycleStatus.TERMINATED,
                }
                and attempt.started_at is None
                and execution.started_at is None
                and execution.cancel_requested_at is None
                and permit.status in {OfflinePermitStatus.ISSUED, OfflinePermitStatus.REVOKED}
            ):
                report = self._record_cancelled_locked(
                    uow,
                    terminal_id=terminal_id,
                    attempt_id=attempt_id,
                    package_id=package_id,
                    report_id=report_id,
                    expected_hash=expected_hash,
                    occurred_at=occurred_at,
                    now=now,
                )
                uow.commit()
                return PrestartFailureResult(execution, attempt, report)
            if (
                package.status is not PackageStatus.ACCEPTED
                or attempt.status is not AttemptStatus.QUEUED
                or execution.status is not ExecutionStatus.QUEUED
                or execution.cancel_requested_at is not None
                or permit.status is not OfflinePermitStatus.ISSUED
            ):
                raise ConflictError("prestart_not_settleable", "Attempt is no longer queued")

            updated_attempt, updated_execution, report = self._settle_expired_locked(
                uow,
                package=package,
                attempt=attempt,
                execution=execution,
                permit=permit,
                terminal_id=terminal_id,
                report_id=report_id,
                expected_hash=expected_hash,
                occurred_at=occurred_at,
                correlation_id=correlation_id,
                now=now,
            )
            uow.commit()
            return PrestartFailureResult(updated_execution, updated_attempt, report)

    @staticmethod
    def _record_cancelled_locked(
        uow: SchedulingUnitOfWork,
        *,
        terminal_id: UUID,
        attempt_id: UUID,
        package_id: UUID,
        report_id: UUID,
        expected_hash: str,
        occurred_at: datetime,
        now: datetime,
    ) -> TerminalReportRecord:
        report = TerminalReportRecord(
            terminal_id=terminal_id,
            report_id=report_id,
            report_kind=TerminalReportKind.ATTEMPT_RESULT,
            disposition=TerminalReportDisposition.STALE,
            command_id=None,
            attempt_id=attempt_id,
            package_id=package_id,
            lease_id=None,
            error_code=CANCELLED_CODE,
            diagnostic=None,
            payload_hash=expected_hash,
            occurred_at=occurred_at,
            received_at=now,
        )
        uow.terminal_reports.add(report)
        return report

    @staticmethod
    def _settle_expired_locked(
        uow: SchedulingUnitOfWork,
        *,
        package: TaskPackageRecord,
        attempt: ExecutionAttemptRecord,
        execution: ExecutionRecord,
        permit: OfflineStartPermitRecord,
        terminal_id: UUID,
        report_id: UUID,
        expected_hash: str,
        occurred_at: datetime,
        correlation_id: UUID,
        now: datetime,
    ) -> tuple[ExecutionAttemptRecord, ExecutionRecord, TerminalReportRecord]:
        if (
            uow.packages.cancel(package.package_id, package.row_version, cancelled_at=now) is None
            or uow.offline_permits.revoke(permit.permit_id, permit.row_version, revoked_at=now)
            is None
        ):
            raise ConflictError("prestart_settlement_conflict", "Delivery changed concurrently")
        updated_attempt, updated_execution = settle_delivery_failure(
            uow,
            execution=execution,
            attempt=attempt,
            error_code=ERROR_CODE,
            correlation_id=correlation_id,
            now=now,
        )
        uow.commands.supersede_pending(attempt.attempt_id, acknowledged_at=now)
        report = TerminalReportRecord(
            terminal_id=terminal_id,
            report_id=report_id,
            report_kind=TerminalReportKind.ATTEMPT_RESULT,
            disposition=TerminalReportDisposition.ACCEPTED,
            command_id=None,
            attempt_id=attempt.attempt_id,
            package_id=package.package_id,
            lease_id=None,
            error_code=ERROR_CODE,
            diagnostic=None,
            payload_hash=expected_hash,
            occurred_at=occurred_at,
            received_at=now,
        )
        uow.terminal_reports.add(report)
        uow.transitions.add_many(
            [
                terminal_transition(
                    attempt.attempt_id,
                    "execution_attempt",
                    attempt.status.value,
                    AttemptStatus.ENDED.value,
                    correlation_id,
                    now,
                ),
                terminal_transition(
                    execution.execution_id,
                    "execution",
                    execution.status.value,
                    ExecutionStatus.ENDED.value,
                    correlation_id,
                    now,
                ),
            ]
        )
        record_delivery_event(
            uow,
            event_type="execution.prestart_permit_expired.v1",
            aggregate_type="execution",
            aggregate_id=execution.execution_id,
            correlation_id=correlation_id,
            occurred_at=now,
            reason_code=ERROR_CODE,
            payload={
                "terminal_id": str(terminal_id),
                "attempt_id": str(attempt.attempt_id),
                "package_id": str(package.package_id),
                "report_id": str(report_id),
            },
        )
        return updated_attempt, updated_execution, report
