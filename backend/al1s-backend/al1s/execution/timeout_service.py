from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from al1s.execution.delivery_types import CommandKind, CommandStatus, TerminalCommandRecord
from al1s.execution.errors import ConflictError
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_state import (
    require_attempt_transition,
    require_execution_transition,
)
from al1s.execution.scheduling_types import (
    AttemptStatus,
    CleanupDeadlineCandidate,
    ExecutionAttemptRecord,
    ExecutionRecord,
    ExecutionStatus,
    FailurePhase,
    StateTransitionRecord,
    TaskLifecycleStatus,
)
from al1s.execution.task_settlement import settle_task_after_execution
from al1s.execution.types import LeaseOwnerKind
from al1s.kernel.types import NewAuditEntry, NewOutboxEvent

CLEANUP_GRACE = timedelta(seconds=60)


class ExecutionTimeoutService:
    def __init__(
        self,
        uow_factory: Callable[[], SchedulingUnitOfWork],
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now

    def request_timed_out_cancellation(self, *, limit: int = 50) -> int:
        now = self._now()
        with self._uow_factory() as uow:
            # The claim query preloads attempt/package identifiers for the bounded batch.
            # Per-candidate writes stay separate to preserve optimistic concurrency and
            # an independent command/audit trail for each execution.
            executions = uow.executions.list_runtime_timeouts_for_update(now=now, limit=limit)
            for candidate in executions:
                execution = candidate.execution
                correlation_id = uuid4()
                updated = uow.executions.request_cancel(
                    execution.execution_id,
                    execution.row_version,
                    now,
                    reason="timeout",
                    deadline_at=now + CLEANUP_GRACE,
                )
                if updated is None:
                    raise ConflictError(
                        "execution_timeout_conflict", "Execution timeout changed concurrently"
                    )
                command = TerminalCommandRecord(
                    command_id=uuid4(),
                    terminal_id=execution.terminal_id,
                    command_kind=CommandKind.CANCEL_REQUESTED,
                    package_id=candidate.package_id,
                    attempt_id=candidate.attempt_id,
                    delivery_no=1,
                    status=CommandStatus.PENDING,
                    payload={
                        "protocol_version": candidate.protocol_version,
                        "reason": "timeout",
                        "cleanup_grace_seconds": 60,
                    },
                    available_at=now,
                    acknowledged_at=None,
                    created_at=now,
                    row_version=1,
                )
                uow.commands.add(command)
                _record(
                    uow,
                    execution_id=execution.execution_id,
                    terminal_id=execution.terminal_id,
                    attempt_id=candidate.attempt_id,
                    command_id=command.command_id,
                    event_type="execution.cancel_requested.v1",
                    reason="execution_timeout",
                    now=now,
                    correlation_id=correlation_id,
                )
            uow.commit()
        return len(executions)

    def force_expired_cleanup(self, *, limit: int = 50) -> int:
        now = self._now()
        with self._uow_factory() as uow:
            # The claim query preloads active attempts and lease versions. Task/schedule
            # settlement remains per execution because each aggregate has its own version.
            executions = uow.executions.list_cleanup_deadlines_for_update(now=now, limit=limit)
            for candidate in executions:
                self._force_one_cleanup(uow, candidate, now=now)
            uow.commit()
        return len(executions)

    def _force_one_cleanup(
        self,
        uow: SchedulingUnitOfWork,
        candidate: CleanupDeadlineCandidate,
        *,
        now: datetime,
    ) -> None:
        correlation_id = uuid4()
        execution = candidate.execution
        attempt = candidate.attempt
        updated_attempt, updated_execution, target_execution = self._mark_cleanup_terminal_state(
            uow,
            execution=execution,
            attempt=attempt,
            now=now,
        )
        self._release_attempt_lease(uow, candidate, now=now)
        uow.commands.supersede_pending(attempt.attempt_id, acknowledged_at=now)
        schedule = uow.schedules.get_by_task_for_update(execution.task_request_id)
        is_operator_cancel = execution.cancel_reason == "operator"
        settle_task_after_execution(
            uow,
            task_id=execution.task_request_id,
            schedule=schedule,
            correlation_id=correlation_id,
            now=now,
            task_target_override=(TaskLifecycleStatus.CANCELLED if is_operator_cancel else None),
        )
        uow.transitions.add_many(
            [
                _transition(
                    "execution_attempt",
                    attempt.attempt_id,
                    attempt.status.value,
                    updated_attempt.status.value,
                    now,
                    correlation_id,
                ),
                _transition(
                    "execution",
                    execution.execution_id,
                    execution.status.value,
                    updated_execution.status.value,
                    now,
                    correlation_id,
                ),
            ]
        )
        _record(
            uow,
            execution_id=execution.execution_id,
            terminal_id=execution.terminal_id,
            attempt_id=attempt.attempt_id,
            command_id=None,
            event_type=f"execution.{target_execution.value}.v1",
            reason="cleanup_deadline_exceeded",
            now=now,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _mark_cleanup_terminal_state(
        uow: SchedulingUnitOfWork,
        *,
        execution: ExecutionRecord,
        attempt: ExecutionAttemptRecord,
        now: datetime,
    ) -> tuple[ExecutionAttemptRecord, ExecutionRecord, ExecutionStatus]:
        is_operator_cancel = execution.cancel_reason == "operator"
        target_attempt = AttemptStatus.CANCELLED if is_operator_cancel else AttemptStatus.TIMED_OUT
        target_execution = (
            ExecutionStatus.CANCELLED if is_operator_cancel else ExecutionStatus.TIMED_OUT
        )
        require_attempt_transition(attempt.status, target_attempt)
        require_execution_transition(execution.status, target_execution)
        updated_attempt = uow.attempts.set_status(
            attempt.attempt_id,
            attempt.row_version,
            target_attempt.value,
            result=None,
            now=now,
            error_code=(
                "CANCEL_CLEANUP_DEADLINE_EXCEEDED"
                if is_operator_cancel
                else "TIMEOUT_CLEANUP_DEADLINE_EXCEEDED"
            ),
            retryable=False,
            failure_phase=FailurePhase.CLEANUP.value,
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
            raise ConflictError(
                "execution_cleanup_conflict", "Execution cleanup changed concurrently"
            )
        return updated_attempt, updated_execution, target_execution

    @staticmethod
    def _release_attempt_lease(
        uow: SchedulingUnitOfWork,
        candidate: CleanupDeadlineCandidate,
        *,
        now: datetime,
    ) -> None:
        attempt = candidate.attempt
        if candidate.lease_id is None or candidate.lease_version is None:
            return
        uow.leases.release(
            candidate.lease_id,
            candidate.lease_version,
            LeaseOwnerKind.EXECUTION_ATTEMPT,
            attempt.attempt_id,
            now,
        )


def _transition(
    aggregate_type: str,
    aggregate_id: UUID,
    from_status: str,
    to_status: str,
    now: datetime,
    correlation_id: UUID,
) -> StateTransitionRecord:
    return StateTransitionRecord(
        transition_id=uuid4(),
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        from_status=from_status,
        to_status=to_status,
        actor_type="system",
        actor_id=None,
        reason_code="cleanup_deadline_exceeded",
        correlation_id=correlation_id,
        occurred_at=now,
    )


def _record(
    uow: SchedulingUnitOfWork,
    *,
    execution_id: UUID,
    terminal_id: UUID,
    attempt_id: UUID,
    command_id: UUID | None,
    event_type: str,
    reason: str,
    now: datetime,
    correlation_id: UUID,
) -> None:
    payload = {
        "terminal_id": str(terminal_id),
        "execution_id": str(execution_id),
        "attempt_id": str(attempt_id),
        "command_id": str(command_id) if command_id else None,
        "reason": reason,
    }
    uow.outbox.add(
        NewOutboxEvent(
            event_id=uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_type="execution",
            aggregate_id=execution_id,
            correlation_id=correlation_id,
            occurred_at=now,
            payload=payload,
        )
    )
    uow.audit.add(
        NewAuditEntry(
            audit_id=uuid4(),
            actor_type="system",
            actor_id=None,
            action=event_type.removesuffix(".v1"),
            target_type="execution",
            target_id=execution_id,
            correlation_id=correlation_id,
            details={"reason_code": reason, **payload},
            summary=reason,
        )
    )
