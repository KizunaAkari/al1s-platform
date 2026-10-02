from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from al1s.execution.delivery_types import (
    CommandKind,
    CommandStatus,
    TerminalCommandRecord,
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
    OccurrenceStatus,
    StateTransitionRecord,
    TaskCancellation,
    TaskLifecycleStatus,
    TaskRequestRecord,
    TaskType,
)
from al1s.kernel.types import NewAuditEntry, NewOutboxEvent


class TaskLifecycleOperations:
    """State-changing task operations kept separate from scheduling/materialization."""

    def __init__(
        self,
        uow_factory: Callable[[], SchedulingUnitOfWork],
        now: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now

    def cancel_single_task(
        self, task_id: UUID, *, expected_version: int, correlation_id: UUID
    ) -> TaskCancellation:
        now = self._now()
        with self._uow_factory() as uow:
            task = self._active_single_task(uow, task_id, expected_version)
            execution = uow.executions.get_by_task_for_update(task_id)
            if task.task_type is TaskType.BATCH:
                uow.occurrences.cancel_planned_for_task(task_id, now)
                if execution and execution.status in {
                    ExecutionStatus.ENDED,
                    ExecutionStatus.CANCELLED,
                    ExecutionStatus.TIMED_OUT,
                }:
                    execution = None
            if execution is not None and (
                execution.status is ExecutionStatus.RUNNING
                or (
                    execution.status is ExecutionStatus.QUEUED
                    and self._has_live_offline_permit(uow, execution, now)
                )
            ):
                result = self._request_running_cancel(uow, task, execution, correlation_id, now)
                uow.commit()
                return result
            if execution is not None and execution.status not in {
                ExecutionStatus.WAITING,
                ExecutionStatus.QUEUED,
            }:
                raise ConflictError(
                    "task_not_cancellable", "Single task is already in a terminal state"
                )
            result = self._cancel_not_running(uow, task, execution, correlation_id, now)
            uow.commit()
            return result

    def delete_history(self, task_id: UUID, *, expected_version: int, correlation_id: UUID) -> None:
        now = self._now()
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id, for_update=True)
            if task is None:
                raise NotFoundError("task")
            if task.row_version != expected_version:
                raise ConflictError("stale_task_version", "Task changed concurrently")
            if task.lifecycle_status is TaskLifecycleStatus.ACTIVE:
                raise ConflictError(
                    "active_task_delete_forbidden",
                    "Active tasks must be cancelled or terminated before deleting history",
                )
            deleted = uow.tasks.soft_delete(task_id, expected_version, now)
            if deleted is None:
                raise ConflictError("stale_task_version", "Task changed concurrently")
            _record(
                uow,
                event_type="execution.task_history_deleted.v1",
                task_id=task_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={"task_id": str(task_id)},
                action="execution.task.history_delete",
                reason_code="task_history_deleted",
            )
            uow.commit()

    @staticmethod
    def _active_single_task(
        uow: SchedulingUnitOfWork, task_id: UUID, expected_version: int
    ) -> TaskRequestRecord:
        task = uow.tasks.get(task_id, for_update=True)
        if task is None:
            raise NotFoundError("task")
        if task.row_version != expected_version:
            raise ConflictError("stale_task_version", "Task changed concurrently")
        if task.task_type not in {TaskType.SINGLE, TaskType.BATCH}:
            raise ConflictError(
                "task_cancel_requires_single",
                "Loop and timed tasks must be terminated through their schedule",
            )
        if task.lifecycle_status is not TaskLifecycleStatus.ACTIVE:
            raise ConflictError("task_not_cancellable", "Task is already in a terminal state")
        return task

    def _request_running_cancel(
        self,
        uow: SchedulingUnitOfWork,
        task: TaskRequestRecord,
        execution: ExecutionRecord,
        correlation_id: UUID,
        now: datetime,
    ) -> TaskCancellation:
        if execution.cancel_requested_at is not None:
            return TaskCancellation(task, execution, True)
        updated = uow.executions.request_cancel(
            execution.execution_id,
            execution.row_version,
            now,
            reason="operator",
            # Start the cooperative cleanup grace only after the terminal confirms
            # that it received a cancellation for an actively running task.
            deadline_at=None,
        )
        if updated is None:
            raise ConflictError("execution_cancel_conflict", "Execution changed concurrently")
        attempt = uow.attempts.get_active_for_execution(execution.execution_id)
        if attempt is None:
            raise ConflictError("execution_attempt_missing", "Active execution attempt is missing")
        package = uow.packages.get_by_attempt_for_update(attempt.attempt_id)
        if package is None:
            raise ConflictError("task_package_missing", "Active task package is missing")
        command = TerminalCommandRecord(
            command_id=uuid4(),
            terminal_id=execution.terminal_id,
            command_kind=CommandKind.CANCEL_REQUESTED,
            package_id=package.package_id,
            attempt_id=attempt.attempt_id,
            delivery_no=1,
            status=CommandStatus.PENDING,
            payload={
                "protocol_version": package.protocol_version,
                "reason": "operator",
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
            event_type="execution.cancel_requested.v1",
            task_id=task.task_id,
            correlation_id=correlation_id,
            occurred_at=now,
            payload={
                "task_id": str(task.task_id),
                "terminal_id": str(execution.terminal_id),
                "execution_id": str(execution.execution_id),
                "attempt_id": str(attempt.attempt_id),
                "command_id": str(command.command_id),
            },
            action="execution.task.cancel",
            reason_code="execution_cancel_requested",
        )
        return TaskCancellation(task, updated, True)

    @staticmethod
    def _has_live_offline_permit(
        uow: SchedulingUnitOfWork,
        execution: ExecutionRecord,
        now: datetime,
    ) -> bool:
        attempt = uow.attempts.get_active_for_execution(execution.execution_id)
        if attempt is None:
            return False
        permit = uow.offline_permits.get_by_attempt_for_update(attempt.attempt_id)
        return (
            permit is not None
            and permit.status is OfflinePermitStatus.ISSUED
            and permit.expires_at > now
        )

    def _cancel_not_running(
        self,
        uow: SchedulingUnitOfWork,
        task: TaskRequestRecord,
        execution: ExecutionRecord | None,
        correlation_id: UUID,
        now: datetime,
    ) -> TaskCancellation:
        transitions: list[StateTransitionRecord] = []
        for occurrence in uow.occurrences.cancel_planned_for_task(task.task_id, now):
            transitions.append(
                _transition(
                    "plan_occurrence",
                    occurrence.occurrence_id,
                    OccurrenceStatus.PLANNED.value,
                    OccurrenceStatus.CANCELLED.value,
                    "task_cancelled",
                    correlation_id,
                    now,
                )
            )
        updated_execution = execution
        if execution is not None:
            updated_execution = self._cancel_queued_execution(
                uow, execution, transitions, correlation_id, now
            )
        updated_task = uow.tasks.set_lifecycle(
            task.task_id,
            task.row_version,
            TaskLifecycleStatus.CANCELLED.value,
            now,
        )
        if updated_task is None:
            raise ConflictError("task_state_conflict", "Task changed concurrently")
        transitions.append(
            _transition(
                "task_request",
                task.task_id,
                task.lifecycle_status.value,
                updated_task.lifecycle_status.value,
                "task_cancelled",
                correlation_id,
                now,
            )
        )
        uow.transitions.add_many(transitions)
        _record(
            uow,
            event_type="execution.task_cancelled.v1",
            task_id=task.task_id,
            correlation_id=correlation_id,
            occurred_at=now,
            payload={
                "task_id": str(task.task_id),
                "execution_id": str(execution.execution_id) if execution else None,
            },
            action="execution.task.cancel",
            reason_code="task_cancelled",
        )
        return TaskCancellation(updated_task, updated_execution, False)

    @staticmethod
    def _cancel_queued_execution(
        uow: SchedulingUnitOfWork,
        execution: ExecutionRecord,
        transitions: list[StateTransitionRecord],
        correlation_id: UUID,
        now: datetime,
    ) -> ExecutionRecord:
        active_attempt = uow.attempts.get_active_for_execution(execution.execution_id)
        if active_attempt is not None:
            require_attempt_transition(active_attempt.status, AttemptStatus.CANCELLED)
            updated_attempt = uow.attempts.set_status(
                active_attempt.attempt_id,
                active_attempt.row_version,
                AttemptStatus.CANCELLED.value,
                result=None,
                now=now,
                error_code=None,
                retryable=None,
                failure_phase=None,
            )
            if updated_attempt is None:
                raise ConflictError("attempt_cancel_conflict", "Attempt changed concurrently")
            transitions.append(
                _attempt_cancel_transition(active_attempt, updated_attempt, correlation_id, now)
            )
        require_execution_transition(execution.status, ExecutionStatus.CANCELLED)
        updated = uow.executions.set_status(
            execution.execution_id,
            execution.row_version,
            ExecutionStatus.CANCELLED.value,
            result=None,
            now=now,
            timeout_at=None,
        )
        if updated is None:
            raise ConflictError("execution_cancel_conflict", "Execution changed concurrently")
        transitions.append(
            _transition(
                "execution",
                execution.execution_id,
                execution.status.value,
                updated.status.value,
                "task_cancelled",
                correlation_id,
                now,
            )
        )
        return updated


def _attempt_cancel_transition(
    current: ExecutionAttemptRecord,
    updated: ExecutionAttemptRecord,
    correlation_id: UUID,
    now: datetime,
) -> StateTransitionRecord:
    return _transition(
        "execution_attempt",
        current.attempt_id,
        current.status.value,
        updated.status.value,
        "task_cancelled",
        correlation_id,
        now,
    )


def _transition(
    aggregate_type: str,
    aggregate_id: UUID,
    from_status: str | None,
    to_status: str,
    reason_code: str,
    correlation_id: UUID,
    occurred_at: datetime,
) -> StateTransitionRecord:
    return StateTransitionRecord(
        transition_id=uuid4(),
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        from_status=from_status,
        to_status=to_status,
        actor_type="operator",
        actor_id=None,
        reason_code=reason_code,
        correlation_id=correlation_id,
        occurred_at=occurred_at,
    )


def _record(
    uow: SchedulingUnitOfWork,
    *,
    event_type: str,
    task_id: UUID,
    correlation_id: UUID,
    occurred_at: datetime,
    payload: dict[str, object],
    action: str,
    reason_code: str,
) -> None:
    uow.outbox.add(
        NewOutboxEvent(
            event_id=uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_type="task_request",
            aggregate_id=task_id,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
            payload=dict(payload),
        )
    )
    uow.audit.add(
        NewAuditEntry(
            audit_id=uuid4(),
            actor_type="operator",
            actor_id=None,
            action=action,
            target_type="task_request",
            target_id=task_id,
            correlation_id=correlation_id,
            details={"reason_code": reason_code, **payload},
            summary=reason_code,
        )
    )
