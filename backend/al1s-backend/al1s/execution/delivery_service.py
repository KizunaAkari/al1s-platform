from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from al1s.execution.attempt_result import AttemptResultMixin
from al1s.execution.attempt_start import AttemptStartMixin
from al1s.execution.command_acknowledgement import CommandAcknowledgementMixin
from al1s.execution.delivery_failure import settle_delivery_failure
from al1s.execution.delivery_support import (
    payload_hash as _payload_hash,
)
from al1s.execution.delivery_support import (
    record_delivery_event as _record,
)
from al1s.execution.delivery_support import (
    require_same_report as _require_same_report,
)
from al1s.execution.delivery_types import (
    CommandKind,
    CommandStatus,
    PackageReceiptDisposition,
    PackageReceiptResult,
    PackageRedeliveryResult,
    PackageStatus,
    RejectionDisposition,
    TaskPackageRecord,
    TerminalCommandRecord,
    TerminalReportDisposition,
    TerminalReportKind,
    TerminalReportRecord,
    classify_rejection,
)
from al1s.execution.errors import ConflictError, InvalidRequestError, NotFoundError
from al1s.execution.offline_permits import (
    OfflinePermitSigner,
    OfflinePermitStatus,
    OfflineStartPermitGrant,
    OfflineStartPermitRecord,
)
from al1s.execution.prestart_failure import PrestartFailureMixin
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_state import (
    require_execution_transition,
)
from al1s.execution.scheduling_types import (
    ExecutionAttemptRecord,
    ExecutionRecord,
    ExecutionStatus,
)

MAX_DELIVERY_ATTEMPTS = 8
MAX_REJECTION_DIAGNOSTIC = 512
CLEANUP_GRACE = timedelta(seconds=60)
DEFAULT_OFFLINE_PERMIT_TTL = timedelta(hours=24)


class TerminalDeliveryService(
    CommandAcknowledgementMixin,
    AttemptStartMixin,
    AttemptResultMixin,
    PrestartFailureMixin,
):
    def __init__(
        self,
        uow_factory: Callable[[], SchedulingUnitOfWork],
        *,
        now: Callable[[], datetime],
        offline_permit_signer: OfflinePermitSigner,
        offline_permit_ttl: timedelta = DEFAULT_OFFLINE_PERMIT_TTL,
    ) -> None:
        if offline_permit_ttl <= timedelta(0):
            raise ValueError("offline permit TTL must be positive")
        self._uow_factory = uow_factory
        self._now = now
        self._offline_permit_signer = offline_permit_signer
        self._offline_permit_ttl = offline_permit_ttl

    def list_commands(self, terminal_id: UUID, *, limit: int = 50) -> list[TerminalCommandRecord]:
        with self._uow_factory() as uow:
            return uow.commands.list_pending(terminal_id, now=self._now(), limit=limit)

    def get_package(self, terminal_id: UUID, package_id: UUID) -> TaskPackageRecord:
        with self._uow_factory() as uow:
            package = uow.packages.get(package_id)
        if package is None or package.terminal_id != terminal_id:
            raise NotFoundError("task_package")
        return package

    def redeliver_blocked_execution(
        self,
        execution_id: UUID,
        *,
        expected_version: int,
        correlation_id: UUID,
    ) -> PackageRedeliveryResult:
        now = self._now()
        with self._uow_factory() as uow:
            execution = uow.executions.get_for_update(execution_id)
            if execution is None:
                raise NotFoundError("execution")
            updated_execution = uow.executions.touch_waiting(execution_id, expected_version)
            if updated_execution is None:
                raise ConflictError(
                    "execution_redelivery_conflict",
                    "Only the current waiting execution can be redelivered",
                )
            attempt = uow.attempts.get_active_for_execution(execution_id)
            if attempt is None:
                raise ConflictError(
                    "execution_attempt_missing", "Active execution attempt is missing"
                )
            package = uow.packages.get_by_attempt_for_update(attempt.attempt_id)
            if package is None:
                raise ConflictError("task_package_missing", "Task package is missing")
            updated_package = uow.packages.prepare_blocked_redelivery(
                package.package_id,
                package.row_version,
                next_delivery_at=now,
            )
            if updated_package is None:
                raise ConflictError(
                    "task_package_not_blocked",
                    "Only a blocked waiting package can be redelivered manually",
                )
            command = TerminalCommandRecord(
                command_id=uuid4(),
                terminal_id=package.terminal_id,
                command_kind=CommandKind.TASK_PACKAGE_AVAILABLE,
                package_id=package.package_id,
                attempt_id=package.attempt_id,
                delivery_no=updated_package.delivery_attempt_count,
                status=CommandStatus.PENDING,
                payload={
                    "protocol_version": package.protocol_version,
                    "package_id": str(package.package_id),
                    "package_hash": package.package_hash,
                },
                available_at=now,
                acknowledged_at=None,
                created_at=now,
                row_version=1,
            )
            uow.commands.add(command)
            _record(
                uow,
                event_type="task_package.redelivery_requested.v1",
                aggregate_type="task_package",
                aggregate_id=package.package_id,
                correlation_id=correlation_id,
                occurred_at=now,
                reason_code="blocked_package_redelivery_requested",
                payload={
                    "execution_id": str(execution_id),
                    "attempt_id": str(attempt.attempt_id),
                    "terminal_id": str(package.terminal_id),
                    "command_id": str(command.command_id),
                },
                actor_type="platform_user",
            )
            uow.commit()
        return PackageRedeliveryResult(updated_package, updated_execution, command)

    def receive_package(
        self,
        *,
        terminal_id: UUID,
        package_id: UUID,
        attempt_id: UUID,
        report_id: UUID,
        command_id: UUID | None,
        disposition: PackageReceiptDisposition,
        rejection_code: str | None,
        diagnostic: str | None,
        occurred_at: datetime,
        correlation_id: UUID,
    ) -> PackageReceiptResult:
        if diagnostic is not None and len(diagnostic) > MAX_REJECTION_DIAGNOSTIC:
            raise InvalidRequestError(
                "terminal_diagnostic_too_long",
                f"Terminal diagnostic must not exceed {MAX_REJECTION_DIAGNOSTIC} characters",
            )
        payload = {
            "package_id": str(package_id),
            "attempt_id": str(attempt_id),
            "command_id": str(command_id) if command_id else None,
            "disposition": disposition.value,
            "rejection_code": rejection_code,
            "diagnostic": diagnostic,
        }
        payload_hash = _payload_hash(payload)
        now = self._now()
        with self._uow_factory() as uow:
            uow.terminal_reports.lock_idempotency_key(terminal_id, report_id)
            existing = uow.terminal_reports.get(terminal_id, report_id)
            if existing is not None:
                _require_same_report(existing, payload_hash)
                return self._existing_receipt(uow, existing, package_id)
            package, execution, attempt, command = self._load_package_receipt_context(
                uow,
                terminal_id=terminal_id,
                attempt_id=attempt_id,
                package_id=package_id,
                command_id=command_id,
            )
            updated_package, report_disposition, event_type = self._apply_package_receipt(
                uow,
                package=package,
                execution=execution,
                attempt=attempt,
                disposition=disposition,
                rejection_code=rejection_code,
                diagnostic=diagnostic,
                correlation_id=correlation_id,
                now=now,
            )
            updated_command = self._acknowledge_command(uow, command, now)
            permit: OfflineStartPermitGrant | None = None
            if disposition in {
                PackageReceiptDisposition.ACCEPTED,
                PackageReceiptDisposition.DUPLICATE,
            }:
                uow.commands.supersede_pending(
                    package.attempt_id,
                    acknowledged_at=now,
                    exclude_command_ids=(command.command_id,) if command else (),
                )
            current_execution = uow.executions.get(package.execution_id)
            if current_execution is None:
                raise ConflictError(
                    "task_package_state_incomplete", "Task package state is incomplete"
                )
            if disposition in {
                PackageReceiptDisposition.ACCEPTED,
                PackageReceiptDisposition.DUPLICATE,
            }:
                permit = self._ensure_offline_permit(
                    uow,
                    package=updated_package,
                    execution=current_execution,
                    now=now,
                )
            report = self._record_package_receipt(
                uow,
                terminal_id=terminal_id,
                report_id=report_id,
                command_id=command_id,
                attempt_id=attempt_id,
                package_id=package_id,
                disposition=report_disposition,
                error_code=rejection_code,
                diagnostic=diagnostic,
                payload_hash=payload_hash,
                occurred_at=occurred_at,
                event_type=event_type,
                correlation_id=correlation_id,
                now=now,
            )
            uow.commit()
        return PackageReceiptResult(
            updated_package,
            current_execution,
            report,
            updated_command,
            permit,
        )

    def _ensure_offline_permit(
        self,
        uow: SchedulingUnitOfWork,
        *,
        package: TaskPackageRecord,
        execution: ExecutionRecord,
        now: datetime,
    ) -> OfflineStartPermitGrant:
        existing = uow.offline_permits.get_by_attempt_for_update(package.attempt_id)
        if existing is not None:
            return self._offline_permit_signer.grant(existing)
        if execution.cancel_requested_at is not None:
            raise ConflictError(
                "offline_permit_cancelled",
                "Cancelled execution cannot receive an offline start permit",
            )
        unsigned = OfflineStartPermitRecord(
            permit_id=uuid4(),
            attempt_id=package.attempt_id,
            package_id=package.package_id,
            execution_id=package.execution_id,
            terminal_id=package.terminal_id,
            target_device_id=package.target_device_id,
            package_hash=package.package_hash,
            permit_version=1,
            token_digest="",
            status=OfflinePermitStatus.ISSUED,
            issued_at=now,
            expires_at=now + self._offline_permit_ttl,
            consumed_at=None,
            consumed_start_report_id=None,
            revoked_at=None,
            row_version=1,
        )
        permit = replace(
            unsigned,
            token_digest=self._offline_permit_signer.token_digest_for(unsigned),
        )
        uow.offline_permits.add(permit)
        return self._offline_permit_signer.grant(permit)

    def _load_package_receipt_context(
        self,
        uow: SchedulingUnitOfWork,
        *,
        terminal_id: UUID,
        attempt_id: UUID,
        package_id: UUID,
        command_id: UUID | None,
    ) -> tuple[
        TaskPackageRecord,
        ExecutionRecord,
        ExecutionAttemptRecord,
        TerminalCommandRecord | None,
    ]:
        package = uow.packages.get_for_update(package_id)
        if (
            package is None
            or package.terminal_id != terminal_id
            or package.attempt_id != attempt_id
        ):
            raise NotFoundError("task_package")
        execution = uow.executions.get_for_update(package.execution_id)
        attempt = uow.attempts.get_for_update(attempt_id)
        if execution is None or attempt is None:
            raise ConflictError("task_package_state_incomplete", "Task package state is incomplete")
        return package, execution, attempt, self._lock_command(uow, command_id, package)

    def _apply_package_receipt(
        self,
        uow: SchedulingUnitOfWork,
        *,
        package: TaskPackageRecord,
        execution: ExecutionRecord,
        attempt: ExecutionAttemptRecord,
        disposition: PackageReceiptDisposition,
        rejection_code: str | None,
        diagnostic: str | None,
        correlation_id: UUID,
        now: datetime,
    ) -> tuple[TaskPackageRecord, TerminalReportDisposition, str]:
        if disposition in {
            PackageReceiptDisposition.ACCEPTED,
            PackageReceiptDisposition.DUPLICATE,
        }:
            updated_package, _ = self._accept_package(uow, package, execution, now)
            report_disposition = (
                TerminalReportDisposition.ACCEPTED
                if disposition is PackageReceiptDisposition.ACCEPTED
                else TerminalReportDisposition.DUPLICATE
            )
            return updated_package, report_disposition, "task_package.accepted.v1"
        if rejection_code is None:
            raise InvalidRequestError(
                "terminal_rejection_code_required",
                "Rejected package receipt requires a stable rejection code",
            )
        updated_package = self._reject_package(
            uow,
            package=package,
            execution=execution,
            attempt=attempt,
            rejection_code=rejection_code,
            diagnostic=diagnostic,
            correlation_id=correlation_id,
            now=now,
        )
        return (
            updated_package,
            TerminalReportDisposition.REJECTED,
            "task_package.rejected.v1",
        )

    @staticmethod
    def _record_package_receipt(
        uow: SchedulingUnitOfWork,
        *,
        terminal_id: UUID,
        report_id: UUID,
        command_id: UUID | None,
        attempt_id: UUID,
        package_id: UUID,
        disposition: TerminalReportDisposition,
        error_code: str | None,
        diagnostic: str | None,
        payload_hash: str,
        occurred_at: datetime,
        event_type: str,
        correlation_id: UUID,
        now: datetime,
    ) -> TerminalReportRecord:
        report = TerminalReportRecord(
            terminal_id=terminal_id,
            report_id=report_id,
            report_kind=TerminalReportKind.PACKAGE_RECEIPT,
            disposition=disposition,
            command_id=command_id,
            attempt_id=attempt_id,
            package_id=package_id,
            lease_id=None,
            error_code=error_code,
            diagnostic=diagnostic,
            payload_hash=payload_hash,
            occurred_at=occurred_at,
            received_at=now,
        )
        uow.terminal_reports.add(report)
        _record(
            uow,
            event_type=event_type,
            aggregate_type="task_package",
            aggregate_id=package_id,
            correlation_id=correlation_id,
            occurred_at=now,
            reason_code=error_code or "task_package_accepted",
            payload={
                "terminal_id": str(terminal_id),
                "attempt_id": str(attempt_id),
                "package_id": str(package_id),
                "disposition": disposition.value,
                "rejection_code": error_code,
                "diagnostic": diagnostic,
            },
        )
        return report

    @staticmethod
    def _lock_command(
        uow: SchedulingUnitOfWork,
        command_id: UUID | None,
        package: TaskPackageRecord,
    ) -> TerminalCommandRecord | None:
        if command_id is None:
            return None
        command = uow.commands.get_for_update(command_id)
        if (
            command is None
            or command.terminal_id != package.terminal_id
            or command.package_id != package.package_id
            or command.attempt_id != package.attempt_id
        ):
            raise NotFoundError("terminal_command")
        return command

    @staticmethod
    def _acknowledge_command(
        uow: SchedulingUnitOfWork,
        command: TerminalCommandRecord | None,
        now: datetime,
    ) -> TerminalCommandRecord | None:
        if command is None or command.status is not CommandStatus.PENDING:
            return command
        return uow.commands.acknowledge(command.command_id, command.row_version, now)

    @staticmethod
    def _accept_package(
        uow: SchedulingUnitOfWork,
        package: TaskPackageRecord,
        execution: ExecutionRecord,
        now: datetime,
    ) -> tuple[TaskPackageRecord, ExecutionRecord]:
        if package.status is PackageStatus.ACCEPTED:
            return package, execution
        if package.status is not PackageStatus.AVAILABLE:
            raise ConflictError("task_package_not_available", "Task package is not available")
        require_execution_transition(execution.status, ExecutionStatus.QUEUED)
        updated_package = uow.packages.set_accepted(package.package_id, package.row_version, now)
        updated_execution = uow.executions.set_status(
            execution.execution_id,
            execution.row_version,
            ExecutionStatus.QUEUED.value,
            result=None,
            now=now,
            timeout_at=None,
        )
        if updated_package is None or updated_execution is None:
            raise ConflictError("task_package_accept_conflict", "Task package changed concurrently")
        return updated_package, updated_execution

    def _reject_package(
        self,
        uow: SchedulingUnitOfWork,
        *,
        package: TaskPackageRecord,
        execution: ExecutionRecord,
        attempt: ExecutionAttemptRecord,
        rejection_code: str,
        diagnostic: str | None,
        correlation_id: UUID,
        now: datetime,
    ) -> TaskPackageRecord:
        disposition = classify_rejection(rejection_code)
        exhausted = (
            disposition is RejectionDisposition.RETRYABLE_WAIT
            and package.delivery_attempt_count >= MAX_DELIVERY_ATTEMPTS
        )
        terminal_failure = disposition is RejectionDisposition.PERMANENT_FAILURE or exhausted
        status = PackageStatus.DELIVERY_FAILED if terminal_failure else PackageStatus.AVAILABLE
        delay_seconds = min(5 * (2 ** max(package.delivery_attempt_count - 1, 0)), 900)
        next_delivery_at = now + timedelta(seconds=delay_seconds)
        updated = uow.packages.record_rejection(
            package.package_id,
            package.row_version,
            disposition=disposition,
            code=rejection_code,
            diagnostic=diagnostic,
            status=status,
            next_delivery_at=next_delivery_at,
            failed_at=now if terminal_failure else None,
        )
        if updated is None:
            raise ConflictError("task_package_reject_conflict", "Task package changed concurrently")
        if terminal_failure:
            settle_delivery_failure(
                uow,
                execution=execution,
                attempt=attempt,
                error_code=("DELIVERY_RETRY_EXHAUSTED" if exhausted else rejection_code),
                correlation_id=correlation_id,
                now=now,
            )
        elif disposition is RejectionDisposition.RETRYABLE_WAIT:
            command = TerminalCommandRecord(
                command_id=uuid4(),
                terminal_id=package.terminal_id,
                command_kind=CommandKind.TASK_PACKAGE_AVAILABLE,
                package_id=package.package_id,
                attempt_id=package.attempt_id,
                delivery_no=updated.delivery_attempt_count,
                status=CommandStatus.PENDING,
                payload={
                    "protocol_version": package.protocol_version,
                    "package_id": str(package.package_id),
                    "package_hash": package.package_hash,
                },
                available_at=next_delivery_at,
                acknowledged_at=None,
                created_at=now,
                row_version=1,
            )
            uow.commands.add(command)
        return updated

    def _existing_receipt(
        self,
        uow: SchedulingUnitOfWork,
        report: TerminalReportRecord,
        package_id: UUID,
    ) -> PackageReceiptResult:
        package = uow.packages.get(package_id)
        if package is None or package.package_id != report.package_id:
            raise ConflictError("terminal_report_reused", "Report ID belongs to another package")
        execution = uow.executions.get(package.execution_id)
        if execution is None:
            raise ConflictError("task_package_state_incomplete", "Task package state is incomplete")
        command = (
            uow.commands.get_for_update(report.command_id)
            if report.command_id is not None
            else None
        )
        permit = uow.offline_permits.get_by_attempt_for_update(package.attempt_id)
        return PackageReceiptResult(
            package,
            execution,
            report,
            command,
            self._offline_permit_signer.grant(permit) if permit is not None else None,
        )
