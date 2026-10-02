from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from al1s.execution.delivery_support import (
    payload_hash as _payload_hash,
)
from al1s.execution.delivery_support import (
    record_delivery_event as _record,
)
from al1s.execution.delivery_support import (
    require_same_report as _require_same_report,
)
from al1s.execution.delivery_support import (
    terminal_transition as _transition,
)
from al1s.execution.delivery_types import (
    AttemptStartResult,
    PackageStatus,
    TaskPackageRecord,
    TerminalReportDisposition,
    TerminalReportKind,
    TerminalReportRecord,
)
from al1s.execution.errors import ConflictError, NotFoundError
from al1s.execution.offline_permits import (
    OfflinePermitSigner,
    OfflinePermitStatus,
    OfflineStartPermitRecord,
)
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_state import (
    require_attempt_transition,
    require_execution_transition,
)
from al1s.execution.scheduling_types import (
    AttemptStatus,
    ExecutionAttemptRecord,
    ExecutionRecord,
    ExecutionSnapshotRecord,
    ExecutionStatus,
)
from al1s.execution.types import ExecutionLeaseRecord, LeaseKind, LeaseOwnerKind

CLEANUP_GRACE = timedelta(seconds=60)
MAX_TERMINAL_CLOCK_SKEW = timedelta(minutes=5)


class AttemptStartMixin:
    _uow_factory: Callable[[], SchedulingUnitOfWork]
    _now: Callable[[], datetime]
    _offline_permit_signer: OfflinePermitSigner

    def start_attempt(
        self,
        *,
        terminal_id: UUID,
        attempt_id: UUID,
        package_id: UUID,
        offline_permit_id: UUID,
        offline_permit_token: str,
        report_id: UUID,
        occurred_at: datetime,
        correlation_id: UUID,
    ) -> AttemptStartResult:
        payload_hash = _payload_hash(
            {
                "attempt_id": str(attempt_id),
                "package_id": str(package_id),
                "offline_permit_id": str(offline_permit_id),
            }
        )
        now = self._now()
        try:
            with self._uow_factory() as uow:
                uow.terminal_reports.lock_idempotency_key(terminal_id, report_id)
                existing = uow.terminal_reports.get(terminal_id, report_id)
                if existing is not None:
                    _require_same_report(existing, payload_hash)
                    return self._existing_start(uow, existing)
                package, attempt, execution, snapshot = self._load_start_context(
                    uow, terminal_id, attempt_id, package_id
                )
                self._consume_offline_permit(
                    uow,
                    terminal_id=terminal_id,
                    package=package,
                    execution=execution,
                    permit_id=offline_permit_id,
                    permit_token=offline_permit_token,
                    report_id=report_id,
                    occurred_at=occurred_at,
                    now=now,
                )
                lease_expires_at = max(
                    now + CLEANUP_GRACE,
                    occurred_at + timedelta(seconds=snapshot.timeout_seconds) + CLEANUP_GRACE,
                )
                updated_execution, updated_attempt, lease = self._start_locked_attempt(
                    uow,
                    terminal_id=terminal_id,
                    attempt=attempt,
                    execution=execution,
                    timeout_seconds=snapshot.timeout_seconds,
                    started_at=occurred_at,
                    lease_expires_at=lease_expires_at,
                    now=now,
                )
                report = self._record_attempt_start(
                    uow,
                    terminal_id=terminal_id,
                    package=package,
                    attempt=attempt,
                    execution=execution,
                    lease=lease,
                    report_id=report_id,
                    payload_hash=payload_hash,
                    occurred_at=occurred_at,
                    correlation_id=correlation_id,
                    now=now,
                )
                uow.commit()
            return AttemptStartResult(updated_execution, updated_attempt, lease, report)
        except IntegrityError as exc:
            raise ConflictError("execution_resource_busy", "Execution resource is busy") from exc

    def _consume_offline_permit(
        self,
        uow: SchedulingUnitOfWork,
        *,
        terminal_id: UUID,
        package: TaskPackageRecord,
        execution: ExecutionRecord,
        permit_id: UUID,
        permit_token: str,
        report_id: UUID,
        occurred_at: datetime,
        now: datetime,
    ) -> OfflineStartPermitRecord:
        permit = uow.offline_permits.get_by_attempt_for_update(package.attempt_id)
        if (
            permit is None
            or permit.permit_id != permit_id
            or permit.terminal_id != terminal_id
            or permit.package_id != package.package_id
            or permit.execution_id != execution.execution_id
            or permit.target_device_id != execution.target_device_id
            or permit.package_hash != package.package_hash
        ):
            raise ConflictError("offline_permit_binding_mismatch", "Offline permit is invalid")
        if permit.status is not OfflinePermitStatus.ISSUED:
            raise ConflictError("offline_permit_not_usable", "Offline permit is not usable")
        if now >= permit.expires_at or occurred_at >= permit.expires_at:
            raise ConflictError("offline_permit_expired", "Offline permit has expired")
        if (
            occurred_at < permit.issued_at - MAX_TERMINAL_CLOCK_SKEW
            or occurred_at > now + MAX_TERMINAL_CLOCK_SKEW
        ):
            raise ConflictError(
                "offline_start_time_invalid", "Offline start time is outside the accepted range"
            )
        if not self._offline_permit_signer.verify(permit, permit_token):
            raise ConflictError("offline_permit_invalid", "Offline permit is invalid")
        consumed = uow.offline_permits.consume(
            permit.permit_id,
            permit.row_version,
            start_report_id=report_id,
            consumed_at=now,
        )
        if consumed is None:
            raise ConflictError("offline_permit_consume_conflict", "Offline permit changed")
        return consumed

    @staticmethod
    def _existing_start(
        uow: SchedulingUnitOfWork, report: TerminalReportRecord
    ) -> AttemptStartResult:
        attempt = uow.attempts.get_for_update(report.attempt_id)
        if attempt is None or attempt.lease_id is None:
            raise ConflictError("attempt_start_state_incomplete", "Start state is incomplete")
        execution = uow.executions.get_for_update(attempt.execution_id)
        lease = uow.leases.get_for_update(attempt.lease_id)
        if execution is None or lease is None:
            raise ConflictError("attempt_start_state_incomplete", "Start state is incomplete")
        return AttemptStartResult(execution, attempt, lease, report)

    @staticmethod
    def _load_start_context(
        uow: SchedulingUnitOfWork,
        terminal_id: UUID,
        attempt_id: UUID,
        package_id: UUID,
    ) -> tuple[
        TaskPackageRecord,
        ExecutionAttemptRecord,
        ExecutionRecord,
        ExecutionSnapshotRecord,
    ]:
        package = uow.packages.get_for_update(package_id)
        attempt = uow.attempts.get_for_update(attempt_id)
        if (
            package is None
            or package.terminal_id != terminal_id
            or package.attempt_id != attempt_id
            or package.status is not PackageStatus.ACCEPTED
            or attempt is None
        ):
            raise ConflictError("attempt_package_not_accepted", "Attempt package is not accepted")
        execution = uow.executions.get_for_update(attempt.execution_id)
        snapshot = uow.snapshots.get_by_execution(attempt.execution_id)
        if execution is None or snapshot is None:
            raise ConflictError("attempt_state_incomplete", "Attempt state is incomplete")
        if execution.terminal_id != terminal_id:
            raise NotFoundError("execution_attempt")
        return package, attempt, execution, snapshot

    @staticmethod
    def _start_locked_attempt(
        uow: SchedulingUnitOfWork,
        *,
        terminal_id: UUID,
        attempt: ExecutionAttemptRecord,
        execution: ExecutionRecord,
        timeout_seconds: int,
        started_at: datetime,
        lease_expires_at: datetime,
        now: datetime,
    ) -> tuple[ExecutionRecord, ExecutionAttemptRecord, ExecutionLeaseRecord]:
        require_attempt_transition(attempt.status, AttemptStatus.RUNNING)
        require_execution_transition(execution.status, ExecutionStatus.RUNNING)
        if not uow.attempts.is_next_for_resources(
            attempt.attempt_id,
            terminal_id=terminal_id,
            target_device_id=execution.target_device_id,
            now=now,
        ):
            raise ConflictError("attempt_not_next_in_queue", "Attempt is not next in FIFO queue")
        uow.leases.release_expired(
            terminal_id=terminal_id,
            target_device_id=execution.target_device_id,
            now=now,
        )
        if uow.leases.has_active(
            terminal_id=terminal_id,
            target_device_id=execution.target_device_id,
            now=now,
        ):
            raise ConflictError("execution_resource_busy", "Execution resource is busy")
        lease = uow.leases.add(
            uuid4(),
            terminal_id,
            execution.target_device_id,
            LeaseKind.EXECUTION,
            LeaseOwnerKind.EXECUTION_ATTEMPT,
            attempt.attempt_id,
            now,
            lease_expires_at,
        )
        updated_attempt = uow.attempts.set_status(
            attempt.attempt_id,
            attempt.row_version,
            AttemptStatus.RUNNING.value,
            result=None,
            now=started_at,
            error_code=None,
            retryable=None,
            failure_phase=None,
            lease_id=lease.lease_id,
        )
        updated_execution = uow.executions.set_status(
            execution.execution_id,
            execution.row_version,
            ExecutionStatus.RUNNING.value,
            result=None,
            now=started_at,
            timeout_at=started_at + timedelta(seconds=timeout_seconds),
        )
        if updated_attempt is None or updated_execution is None:
            raise ConflictError("attempt_start_conflict", "Attempt changed concurrently")
        return updated_execution, updated_attempt, lease

    @staticmethod
    def _record_attempt_start(
        uow: SchedulingUnitOfWork,
        *,
        terminal_id: UUID,
        package: TaskPackageRecord,
        attempt: ExecutionAttemptRecord,
        execution: ExecutionRecord,
        lease: ExecutionLeaseRecord,
        report_id: UUID,
        payload_hash: str,
        occurred_at: datetime,
        correlation_id: UUID,
        now: datetime,
    ) -> TerminalReportRecord:
        report = TerminalReportRecord(
            terminal_id=terminal_id,
            report_id=report_id,
            report_kind=TerminalReportKind.ATTEMPT_START,
            disposition=TerminalReportDisposition.ACCEPTED,
            command_id=None,
            attempt_id=attempt.attempt_id,
            package_id=package.package_id,
            lease_id=lease.lease_id,
            error_code=None,
            diagnostic=None,
            payload_hash=payload_hash,
            occurred_at=occurred_at,
            received_at=now,
        )
        uow.terminal_reports.add(report)
        uow.transitions.add_many(
            [
                _transition(
                    attempt.attempt_id,
                    "execution_attempt",
                    attempt.status.value,
                    AttemptStatus.RUNNING.value,
                    correlation_id,
                    now,
                ),
                _transition(
                    execution.execution_id,
                    "execution",
                    execution.status.value,
                    ExecutionStatus.RUNNING.value,
                    correlation_id,
                    now,
                ),
            ]
        )
        _record(
            uow,
            event_type="execution.started.v1",
            aggregate_type="execution",
            aggregate_id=execution.execution_id,
            correlation_id=correlation_id,
            occurred_at=now,
            reason_code="execution_attempt_started",
            payload={
                "terminal_id": str(terminal_id),
                "attempt_id": str(attempt.attempt_id),
                "execution_id": str(execution.execution_id),
                "lease_id": str(lease.lease_id),
                "lease_version": lease.row_version,
            },
        )
        return report
