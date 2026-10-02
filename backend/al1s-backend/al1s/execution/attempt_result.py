from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from al1s.execution.command_acknowledgement import CANCEL_DELIVERY_NOTE
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
    TerminalReportDisposition,
    TerminalReportKind,
    TerminalReportRecord,
    TerminalResultKind,
    TerminalResultReceipt,
)
from al1s.execution.diagnostic_events import conditional_skip_metadata
from al1s.execution.errors import ConflictError, InvalidRequestError, NotFoundError
from al1s.execution.package_builder import create_delivery_records
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_state import (
    require_attempt_transition,
    require_execution_transition,
)
from al1s.execution.scheduling_types import (
    AttemptStatus,
    ExecutionAttemptRecord,
    ExecutionRecord,
    ExecutionResult,
    ExecutionSnapshotRecord,
    ExecutionStatus,
    FailurePhase,
    TaskLifecycleStatus,
)
from al1s.execution.task_settlement import settle_task_after_execution
from al1s.execution.types import ExecutionLeaseRecord, LeaseOwnerKind

CLEANUP_GRACE = timedelta(seconds=60)


class AttemptResultMixin:
    _uow_factory: Callable[[], SchedulingUnitOfWork]
    _now: Callable[[], datetime]

    def receive_result(
        self,
        *,
        terminal_id: UUID,
        attempt_id: UUID,
        package_id: UUID,
        report_id: UUID,
        lease_id: UUID,
        lease_version: int,
        result_kind: TerminalResultKind,
        error_code: str | None,
        retryable: bool,
        occurred_at: datetime,
        correlation_id: UUID,
        diagnostic: dict[str, Any] | None = None,
    ) -> TerminalResultReceipt:
        if diagnostic is not None:
            try:
                encoded = json.dumps(diagnostic, ensure_ascii=False, allow_nan=False).encode()
            except (ValueError, TypeError, RecursionError) as exc:
                raise InvalidRequestError(
                    "invalid_execution_diagnostic", "Invalid diagnostic JSON"
                ) from exc
            if len(encoded) > 1048576:
                raise InvalidRequestError(
                    "execution_diagnostic_too_large", "Diagnostic exceeds 1 MiB"
                )
        if error_code is not None and len(error_code) > 100:
            raise InvalidRequestError(
                "invalid_execution_error_code", "Execution error code is too long"
            )
        payload_hash = _payload_hash(
            {
                "attempt_id": str(attempt_id),
                "package_id": str(package_id),
                "lease_id": str(lease_id),
                "lease_version": lease_version,
                "result_kind": result_kind.value,
                "error_code": error_code,
                "retryable": retryable,
                **({"diagnostic": diagnostic} if diagnostic is not None else {}),
            }
        )
        now = self._now()
        with self._uow_factory() as uow:
            uow.terminal_reports.lock_idempotency_key(terminal_id, report_id)
            existing = uow.terminal_reports.get(terminal_id, report_id)
            if existing is not None:
                _require_same_report(existing, payload_hash)
                return self._existing_result(uow, existing)
            attempt, execution, lease = self._load_result_context(
                uow,
                terminal_id=terminal_id,
                attempt_id=attempt_id,
                package_id=package_id,
                lease_id=lease_id,
            )
            if (
                not self._is_current_result_lease(
                    lease,
                    attempt=attempt,
                    lease_version=lease_version,
                )
                or execution.status is not ExecutionStatus.RUNNING
            ):
                report = self._record_stale_result(
                    uow,
                    terminal_id=terminal_id,
                    report_id=report_id,
                    attempt_id=attempt_id,
                    package_id=package_id,
                    lease_id=lease_id,
                    error_code=error_code,
                    payload_hash=payload_hash,
                    occurred_at=occurred_at,
                    received_at=now,
                )
                uow.commit()
                return TerminalResultReceipt(execution, attempt, report, None)
            updated_attempt, updated_execution, retry_attempt, event_type = (
                self._finalize_current_result(
                    uow,
                    execution=execution,
                    attempt=attempt,
                    lease_id=lease_id,
                    lease_version=lease_version,
                    result_kind=result_kind,
                    error_code=error_code,
                    retryable=retryable,
                    correlation_id=correlation_id,
                    now=now,
                )
            )
            report = self._record_accepted_result(
                uow,
                diagnostic=diagnostic,
                terminal_id=terminal_id,
                report_id=report_id,
                package_id=package_id,
                lease_id=lease_id,
                attempt=attempt,
                execution=execution,
                updated_attempt=updated_attempt,
                updated_execution=updated_execution,
                retry_attempt=retry_attempt,
                event_type=event_type,
                result_kind=result_kind,
                error_code=error_code,
                payload_hash=payload_hash,
                occurred_at=occurred_at,
                correlation_id=correlation_id,
                now=now,
            )
            uow.commit()
        return TerminalResultReceipt(updated_execution, updated_attempt, report, retry_attempt)

    @staticmethod
    def _load_result_context(
        uow: SchedulingUnitOfWork,
        *,
        terminal_id: UUID,
        attempt_id: UUID,
        package_id: UUID,
        lease_id: UUID,
    ) -> tuple[ExecutionAttemptRecord, ExecutionRecord, ExecutionLeaseRecord | None]:
        package = uow.packages.get_for_update(package_id)
        attempt = uow.attempts.get_for_update(attempt_id)
        if (
            package is None
            or package.terminal_id != terminal_id
            or package.attempt_id != attempt_id
            or attempt is None
        ):
            raise NotFoundError("execution_attempt")
        execution = uow.executions.get_for_update(attempt.execution_id)
        lease = uow.leases.get_for_update(lease_id)
        if execution is None or execution.terminal_id != terminal_id:
            raise NotFoundError("execution_attempt")
        return attempt, execution, lease

    @staticmethod
    def _is_current_result_lease(
        lease: ExecutionLeaseRecord | None,
        *,
        attempt: ExecutionAttemptRecord,
        lease_version: int,
    ) -> bool:
        return (
            lease is not None
            and lease.lease_id == attempt.lease_id
            and lease.row_version == lease_version
            and lease.owner_kind is LeaseOwnerKind.EXECUTION_ATTEMPT
            and lease.owner_id == attempt.attempt_id
            and lease.released_at is None
        )

    def _finalize_current_result(
        self,
        uow: SchedulingUnitOfWork,
        *,
        execution: ExecutionRecord,
        attempt: ExecutionAttemptRecord,
        lease_id: UUID,
        lease_version: int,
        result_kind: TerminalResultKind,
        error_code: str | None,
        retryable: bool,
        correlation_id: UUID,
        now: datetime,
    ) -> tuple[
        ExecutionAttemptRecord,
        ExecutionRecord,
        ExecutionAttemptRecord | None,
        str,
    ]:
        snapshot = uow.snapshots.get_by_execution(execution.execution_id)
        if snapshot is None:
            raise ConflictError("execution_snapshot_missing", "Execution snapshot is missing")
        schedule = uow.schedules.get_by_task_for_update(execution.task_request_id)
        outcome = self._apply_terminal_result(
            uow,
            execution=execution,
            attempt=attempt,
            snapshot=snapshot,
            schedule_is_active=(schedule is None or schedule.status.value == "active"),
            result_kind=result_kind,
            error_code=error_code,
            retryable=retryable,
            now=now,
        )
        if not uow.leases.release(
            lease_id,
            lease_version,
            LeaseOwnerKind.EXECUTION_ATTEMPT,
            attempt.attempt_id,
            now,
        ):
            raise ConflictError("attempt_lease_changed", "Execution lease changed concurrently")
        uow.commands.supersede_pending(attempt.attempt_id, acknowledged_at=now)
        if outcome[2] is None:
            settle_task_after_execution(
                uow,
                task_id=execution.task_request_id,
                schedule=schedule,
                correlation_id=correlation_id,
                now=now,
                task_target_override=(
                    TaskLifecycleStatus.CANCELLED
                    if result_kind is TerminalResultKind.CANCELLED and schedule is None
                    else None
                ),
            )
        return outcome

    @staticmethod
    def _record_accepted_result(
        uow: SchedulingUnitOfWork,
        *,
        diagnostic: dict[str, Any] | None,
        terminal_id: UUID,
        report_id: UUID,
        package_id: UUID,
        lease_id: UUID,
        attempt: ExecutionAttemptRecord,
        execution: ExecutionRecord,
        updated_attempt: ExecutionAttemptRecord,
        updated_execution: ExecutionRecord,
        retry_attempt: ExecutionAttemptRecord | None,
        event_type: str,
        result_kind: TerminalResultKind,
        error_code: str | None,
        payload_hash: str,
        occurred_at: datetime,
        correlation_id: UUID,
        now: datetime,
    ) -> TerminalReportRecord:
        report = TerminalReportRecord(
            terminal_id=terminal_id,
            report_id=report_id,
            report_kind=TerminalReportKind.ATTEMPT_RESULT,
            disposition=TerminalReportDisposition.ACCEPTED,
            result_diagnostic=diagnostic,
            command_id=None,
            attempt_id=attempt.attempt_id,
            package_id=package_id,
            lease_id=lease_id,
            error_code=error_code,
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
                    updated_attempt.status.value,
                    correlation_id,
                    now,
                ),
                _transition(
                    execution.execution_id,
                    "execution",
                    execution.status.value,
                    updated_execution.status.value,
                    correlation_id,
                    now,
                ),
            ]
        )
        for metadata in conditional_skip_metadata(diagnostic):
            _record(
                uow,
                event_type="execution.conditional_skip.v1",
                aggregate_type="execution",
                aggregate_id=execution.execution_id,
                correlation_id=correlation_id,
                occurred_at=now,
                reason_code="conditional_skip",
                payload={
                    "attempt_id": str(attempt.attempt_id),
                    "report_id": str(report_id),
                    **metadata,
                    "task_id": str(execution.task_request_id),
                    "terminal_id": str(terminal_id),
                },
            )
        _record(
            uow,
            event_type=event_type,
            aggregate_type="execution",
            aggregate_id=execution.execution_id,
            correlation_id=correlation_id,
            occurred_at=now,
            reason_code=f"terminal_result_{result_kind.value}",
            payload={
                "terminal_id": str(terminal_id),
                "attempt_id": str(attempt.attempt_id),
                "execution_id": str(execution.execution_id),
                "result_kind": result_kind.value,
                "task_id": str(execution.task_request_id),
                "retry_attempt_id": (str(retry_attempt.attempt_id) if retry_attempt else None),
            },
        )
        return report

    def _apply_terminal_result(
        self,
        uow: SchedulingUnitOfWork,
        *,
        execution: ExecutionRecord,
        attempt: ExecutionAttemptRecord,
        snapshot: ExecutionSnapshotRecord,
        schedule_is_active: bool,
        result_kind: TerminalResultKind,
        error_code: str | None,
        retryable: bool,
        now: datetime,
    ) -> tuple[
        ExecutionAttemptRecord,
        ExecutionRecord,
        ExecutionAttemptRecord | None,
        str,
    ]:
        if result_kind in {TerminalResultKind.SUCCESS, TerminalResultKind.FAILURE}:
            return self._apply_business_result(
                uow,
                execution=execution,
                attempt=attempt,
                snapshot=snapshot,
                schedule_is_active=schedule_is_active,
                result_kind=result_kind,
                error_code=error_code,
                retryable=retryable,
                now=now,
            )
        return self._apply_stopped_result(
            uow,
            execution=execution,
            attempt=attempt,
            result_kind=result_kind,
            error_code=error_code,
            now=now,
        )

    def _apply_business_result(
        self,
        uow: SchedulingUnitOfWork,
        *,
        execution: ExecutionRecord,
        attempt: ExecutionAttemptRecord,
        snapshot: ExecutionSnapshotRecord,
        schedule_is_active: bool,
        result_kind: TerminalResultKind,
        error_code: str | None,
        retryable: bool,
        now: datetime,
    ) -> tuple[
        ExecutionAttemptRecord,
        ExecutionRecord,
        ExecutionAttemptRecord | None,
        str,
    ]:
        require_attempt_transition(attempt.status, AttemptStatus.ENDED)
        result = (
            ExecutionResult.SUCCESS
            if result_kind is TerminalResultKind.SUCCESS
            else ExecutionResult.FAILURE
        )
        updated_attempt = uow.attempts.set_status(
            attempt.attempt_id,
            attempt.row_version,
            AttemptStatus.ENDED.value,
            result=result.value,
            now=now,
            error_code=error_code,
            retryable=retryable,
            failure_phase=(
                FailurePhase.RUNTIME.value if result is ExecutionResult.FAILURE else None
            ),
        )
        may_retry = (
            result is ExecutionResult.FAILURE
            and retryable
            and attempt.attempt_no <= snapshot.max_retries
            and schedule_is_active
        )
        if may_retry:
            require_execution_transition(execution.status, ExecutionStatus.WAITING)
            updated_execution = uow.executions.set_status(
                execution.execution_id,
                execution.row_version,
                ExecutionStatus.WAITING.value,
                result=None,
                now=now,
                timeout_at=None,
            )
            retry_attempt = self._add_retry_delivery(uow, attempt, snapshot, now)
            event_type = "execution.retry_queued.v1"
        else:
            require_execution_transition(execution.status, ExecutionStatus.ENDED)
            updated_execution = uow.executions.set_status(
                execution.execution_id,
                execution.row_version,
                ExecutionStatus.ENDED.value,
                result=result.value,
                now=now,
                timeout_at=execution.timeout_at,
            )
            retry_attempt = None
            event_type = "execution.ended.v1"
        if updated_attempt is None or updated_execution is None:
            raise ConflictError("attempt_result_conflict", "Execution changed concurrently")
        if execution.cancel_requested_at is not None:
            delayed_execution = uow.executions.acknowledge_cancel_delivery(
                updated_execution.execution_id,
                updated_execution.row_version,
                deadline_at=None,
                delayed=True,
                note=CANCEL_DELIVERY_NOTE,
            )
            if delayed_execution is None:
                raise ConflictError(
                    "attempt_result_conflict", "Cancellation delivery changed concurrently"
                )
            updated_execution = delayed_execution
        return updated_attempt, updated_execution, retry_attempt, event_type

    @staticmethod
    def _apply_stopped_result(
        uow: SchedulingUnitOfWork,
        *,
        execution: ExecutionRecord,
        attempt: ExecutionAttemptRecord,
        result_kind: TerminalResultKind,
        error_code: str | None,
        now: datetime,
    ) -> tuple[
        ExecutionAttemptRecord,
        ExecutionRecord,
        None,
        str,
    ]:
        target_attempt = (
            AttemptStatus.CANCELLED
            if result_kind is TerminalResultKind.CANCELLED
            else AttemptStatus.TIMED_OUT
        )
        target_execution = (
            ExecutionStatus.CANCELLED
            if result_kind is TerminalResultKind.CANCELLED
            else ExecutionStatus.TIMED_OUT
        )
        require_attempt_transition(attempt.status, target_attempt)
        require_execution_transition(execution.status, target_execution)
        updated_attempt = uow.attempts.set_status(
            attempt.attempt_id,
            attempt.row_version,
            target_attempt.value,
            result=None,
            now=now,
            error_code=error_code,
            retryable=False,
            failure_phase=(
                FailurePhase.CLEANUP.value if result_kind is TerminalResultKind.TIMED_OUT else None
            ),
        )
        updated_execution = uow.executions.set_status(
            execution.execution_id,
            execution.row_version,
            target_execution.value,
            result=None,
            now=now,
            timeout_at=execution.timeout_at,
        )
        if updated_attempt is None or updated_execution is None:
            raise ConflictError("attempt_result_conflict", "Execution changed concurrently")
        return updated_attempt, updated_execution, None, f"execution.{target_execution.value}.v1"

    @staticmethod
    def _add_retry_delivery(
        uow: SchedulingUnitOfWork,
        attempt: ExecutionAttemptRecord,
        snapshot: ExecutionSnapshotRecord,
        now: datetime,
    ) -> ExecutionAttemptRecord:
        retry_attempt = ExecutionAttemptRecord(
            attempt_id=uuid4(),
            execution_id=attempt.execution_id,
            attempt_no=attempt.attempt_no + 1,
            status=AttemptStatus.QUEUED,
            result=None,
            available_at=now,
            enqueued_at=now,
            started_at=None,
            ended_at=None,
            error_code=None,
            retryable=None,
            failure_phase=None,
            lease_id=None,
            row_version=1,
        )
        uow.attempts.add(retry_attempt)
        uow.flush()
        package, command = create_delivery_records(
            attempt=retry_attempt,
            snapshot=snapshot,
            resources=uow.snapshots.list_package_resources(snapshot.snapshot_id),
            created_at=now,
        )
        uow.packages.add(package)
        uow.flush()
        uow.commands.add(command)
        return retry_attempt

    @staticmethod
    def _record_stale_result(
        uow: SchedulingUnitOfWork,
        *,
        terminal_id: UUID,
        report_id: UUID,
        attempt_id: UUID,
        package_id: UUID,
        lease_id: UUID,
        error_code: str | None,
        payload_hash: str,
        occurred_at: datetime,
        received_at: datetime,
    ) -> TerminalReportRecord:
        report = TerminalReportRecord(
            terminal_id=terminal_id,
            report_id=report_id,
            report_kind=TerminalReportKind.ATTEMPT_RESULT,
            disposition=TerminalReportDisposition.STALE,
            command_id=None,
            attempt_id=attempt_id,
            package_id=package_id,
            lease_id=lease_id,
            error_code=error_code,
            diagnostic=None,
            payload_hash=payload_hash,
            occurred_at=occurred_at,
            received_at=received_at,
        )
        uow.terminal_reports.add(report)
        return report

    @staticmethod
    def _existing_result(
        uow: SchedulingUnitOfWork,
        report: TerminalReportRecord,
    ) -> TerminalResultReceipt:
        attempt = uow.attempts.get_for_update(report.attempt_id)
        if attempt is None:
            raise ConflictError("terminal_report_state_incomplete", "Report state is incomplete")
        execution = uow.executions.get_for_update(attempt.execution_id)
        if execution is None:
            raise ConflictError("terminal_report_state_incomplete", "Report state is incomplete")
        retry_attempt = uow.attempts.get_active_for_execution(execution.execution_id)
        if retry_attempt is not None and retry_attempt.attempt_id == attempt.attempt_id:
            retry_attempt = None
        return TerminalResultReceipt(execution, attempt, report, retry_attempt)
