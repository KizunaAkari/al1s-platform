from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from al1s.execution.delivery_support import payload_hash, record_delivery_event, terminal_transition
from al1s.execution.delivery_types import (
    CancellationAcknowledgementOutcome,
    CommandAcknowledgementRecord,
    CommandAcknowledgementResult,
    CommandKind,
    CommandStatus,
)
from al1s.execution.errors import ConflictError, NotFoundError
from al1s.execution.offline_permits import OfflinePermitStatus
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_state import (
    require_attempt_transition,
    require_execution_transition,
)
from al1s.execution.scheduling_types import (
    AttemptStatus,
    ExecutionAttemptRecord,
    ExecutionRecord,
    ExecutionStatus,
    TaskLifecycleStatus,
)
from al1s.execution.task_settlement import settle_task_after_execution

CLEANUP_GRACE = timedelta(seconds=60)
CANCEL_DELIVERY_NOTE = "取消请求未能及时送达"


class CommandAcknowledgementMixin:
    _uow_factory: Callable[[], SchedulingUnitOfWork]
    _now: Callable[[], datetime]

    def acknowledge_cancel_command(
        self,
        *,
        terminal_id: UUID,
        command_id: UUID,
        report_id: UUID,
        outcome: CancellationAcknowledgementOutcome,
        occurred_at: datetime,
        correlation_id: UUID,
    ) -> CommandAcknowledgementResult:
        expected_hash = payload_hash(
            {"command_id": str(command_id), "outcome": outcome.value}
        )
        now = self._now()
        with self._uow_factory() as uow:
            uow.terminal_reports.lock_idempotency_key(terminal_id, report_id)
            existing = uow.command_acknowledgements.get(terminal_id, report_id)
            if existing is not None:
                if (
                    existing.command_id != command_id
                    or existing.outcome is not outcome
                    or existing.payload_hash != expected_hash
                ):
                    raise ConflictError(
                        "command_acknowledgement_reused",
                        "Acknowledgement report ID was reused with another payload",
                    )
                return self._existing_acknowledgement(uow, existing)

            command = uow.commands.get_for_update(command_id)
            if (
                command is None
                or command.terminal_id != terminal_id
                or command.command_kind is not CommandKind.CANCEL_REQUESTED
            ):
                raise NotFoundError("terminal_command")
            if command.status is not CommandStatus.PENDING:
                raise ConflictError(
                    "command_not_pending", "Cancellation command is no longer pending"
                )
            attempt = uow.attempts.get_for_update(command.attempt_id)
            if attempt is None:
                raise ConflictError("execution_attempt_missing", "Execution attempt is missing")
            execution = uow.executions.get_for_update(attempt.execution_id)
            if execution is None or execution.cancel_requested_at is None:
                raise ConflictError(
                    "execution_cancel_missing", "Execution has no pending cancellation"
                )

            if outcome is CancellationAcknowledgementOutcome.RUNNING_CANCEL_ACCEPTED:
                execution = self._acknowledge_running(uow, execution, now)
            elif outcome is CancellationAcknowledgementOutcome.ALREADY_COMPLETED:
                execution = self._acknowledge_completed(uow, execution)
            else:
                execution = self._cancel_before_start(
                    uow,
                    execution=execution,
                    attempt=attempt,
                    command_id=command_id,
                    correlation_id=correlation_id,
                    now=now,
                )

            acknowledged_command = uow.commands.acknowledge(
                command.command_id,
                command.row_version,
                now,
            )
            if acknowledged_command is None:
                raise ConflictError("command_acknowledgement_conflict", "Command changed")
            acknowledgement = CommandAcknowledgementRecord(
                terminal_id=terminal_id,
                report_id=report_id,
                command_id=command_id,
                attempt_id=attempt.attempt_id,
                execution_id=execution.execution_id,
                outcome=outcome,
                payload_hash=expected_hash,
                occurred_at=occurred_at,
                received_at=now,
            )
            uow.command_acknowledgements.add(acknowledgement)
            record_delivery_event(
                uow,
                event_type="execution.cancel_command_acknowledged.v1",
                aggregate_type="execution",
                aggregate_id=execution.execution_id,
                correlation_id=correlation_id,
                occurred_at=now,
                reason_code=outcome.value,
                payload={
                    "terminal_id": str(terminal_id),
                    "command_id": str(command_id),
                    "attempt_id": str(attempt.attempt_id),
                    "outcome": outcome.value,
                },
            )
            uow.commit()
        return CommandAcknowledgementResult(
            acknowledged_command,
            execution,
            acknowledgement,
        )

    @staticmethod
    def _acknowledge_running(
        uow: SchedulingUnitOfWork,
        execution: ExecutionRecord,
        now: datetime,
    ) -> ExecutionRecord:
        if execution.status is not ExecutionStatus.RUNNING:
            raise ConflictError(
                "cancel_outcome_mismatch", "Terminal reported running for a non-running task"
            )
        updated = uow.executions.acknowledge_cancel_delivery(
            execution.execution_id,
            execution.row_version,
            deadline_at=now + CLEANUP_GRACE,
            delayed=False,
            note=None,
        )
        if updated is None:
            raise ConflictError("execution_cancel_conflict", "Execution changed concurrently")
        return updated

    @staticmethod
    def _acknowledge_completed(
        uow: SchedulingUnitOfWork,
        execution: ExecutionRecord,
    ) -> ExecutionRecord:
        if execution.status is not ExecutionStatus.RUNNING:
            raise ConflictError(
                "cancel_outcome_mismatch",
                "Terminal reported completion before its start was reconciled",
            )
        updated = uow.executions.acknowledge_cancel_delivery(
            execution.execution_id,
            execution.row_version,
            deadline_at=None,
            delayed=True,
            note=CANCEL_DELIVERY_NOTE,
        )
        if updated is None:
            raise ConflictError("execution_cancel_conflict", "Execution changed concurrently")
        return updated

    @staticmethod
    def _cancel_before_start(
        uow: SchedulingUnitOfWork,
        *,
        execution: ExecutionRecord,
        attempt: ExecutionAttemptRecord,
        command_id: UUID,
        correlation_id: UUID,
        now: datetime,
    ) -> ExecutionRecord:
        if (
            execution.status is not ExecutionStatus.QUEUED
            or attempt.status is not AttemptStatus.QUEUED
        ):
            raise ConflictError(
                "cancel_outcome_mismatch", "Terminal reported not started for an active execution"
            )
        package = uow.packages.get_by_attempt_for_update(attempt.attempt_id)
        permit = uow.offline_permits.get_by_attempt_for_update(attempt.attempt_id)
        if package is None or permit is None:
            raise ConflictError("execution_delivery_missing", "Cancellation delivery is incomplete")
        if permit.status is not OfflinePermitStatus.ISSUED:
            raise ConflictError("offline_permit_not_revocable", "Offline permit was already used")
        require_attempt_transition(attempt.status, AttemptStatus.CANCELLED)
        require_execution_transition(execution.status, ExecutionStatus.CANCELLED)
        updated_attempt = uow.attempts.set_status(
            attempt.attempt_id,
            attempt.row_version,
            AttemptStatus.CANCELLED.value,
            result=None,
            now=now,
            error_code=None,
            retryable=False,
            failure_phase=None,
        )
        updated_execution = uow.executions.set_status(
            execution.execution_id,
            execution.row_version,
            ExecutionStatus.CANCELLED.value,
            result=None,
            now=now,
            timeout_at=None,
        )
        if updated_attempt is None or updated_execution is None:
            raise ConflictError("execution_cancel_conflict", "Execution changed concurrently")
        if uow.packages.cancel(
            package.package_id,
            package.row_version,
            cancelled_at=now,
        ) is None or uow.offline_permits.revoke(
            permit.permit_id,
            permit.row_version,
            revoked_at=now,
        ) is None:
            raise ConflictError("execution_cancel_conflict", "Delivery changed concurrently")
        schedule = uow.schedules.get_by_task_for_update(execution.task_request_id)
        settle_task_after_execution(
            uow,
            task_id=execution.task_request_id,
            schedule=schedule,
            correlation_id=correlation_id,
            now=now,
            task_target_override=(
                TaskLifecycleStatus.CANCELLED if schedule is None else None
            ),
        )
        uow.transitions.add_many(
            [
                terminal_transition(
                    attempt.attempt_id,
                    "execution_attempt",
                    attempt.status.value,
                    updated_attempt.status.value,
                    correlation_id,
                    now,
                ),
                terminal_transition(
                    execution.execution_id,
                    "execution",
                    execution.status.value,
                    updated_execution.status.value,
                    correlation_id,
                    now,
                ),
            ]
        )
        return updated_execution

    @staticmethod
    def _existing_acknowledgement(
        uow: SchedulingUnitOfWork,
        acknowledgement: CommandAcknowledgementRecord,
    ) -> CommandAcknowledgementResult:
        command = uow.commands.get_for_update(acknowledgement.command_id)
        execution = uow.executions.get_for_update(acknowledgement.execution_id)
        if command is None or execution is None:
            raise ConflictError(
                "command_acknowledgement_incomplete", "Acknowledgement state is incomplete"
            )
        return CommandAcknowledgementResult(command, execution, acknowledgement)
