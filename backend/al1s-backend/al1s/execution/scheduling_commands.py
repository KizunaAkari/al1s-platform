from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from al1s.execution.delivery_types import PackageStatus
from al1s.execution.errors import ConflictError, InvalidRequestError, NotFoundError
from al1s.execution.scheduling_base import SchedulingContext
from al1s.execution.scheduling_helpers import (
    _build_plan,
    _request_hash,
    _transition,
)
from al1s.execution.scheduling_state import (
    require_attempt_transition,
    require_execution_transition,
)
from al1s.execution.scheduling_types import (
    AttemptCompletion,
    AttemptStatus,
    CreatedTask,
    CreateTaskCommand,
    ExecutionAttemptRecord,
    ExecutionResult,
    ExecutionStatus,
    ScheduleStatus,
    StateTransitionRecord,
    TaskLifecycleStatus,
    TaskRequestRecord,
)
from al1s.execution.task_settlement import settle_task_after_execution


class SchedulingCommands(SchedulingContext):
    def create_task(self, command: CreateTaskCommand, *, correlation_id: UUID) -> CreatedTask:
        self._validate_command(command)
        request_hash = _request_hash(command)
        idempotency_key = command.idempotency_key.strip()
        # A replay is answered from the persisted request before resolving the
        # current module definition.  Published content may have moved on (or
        # its provider may be temporarily unavailable), but that must not make
        # an already accepted idempotency key fail on retry.
        with self._uow_factory() as uow:
            existing = uow.tasks.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return self._idempotent_result(uow, existing, request_hash)
        self._definitions.resolve(
            command.source_module,
            command.logical_content_id,
            command.parameters,
        )
        self._definitions.authorize_target(
            command.source_module,
            command.logical_content_id,
            command.requested_target_device_id,
        )
        now = self._now()
        task_id = uuid4()
        schedule, schedule_revision, revision_times, occurrences = _build_plan(
            command, task_id, now
        )
        task = TaskRequestRecord(
            task_id=task_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            name=command.name.strip(),
            task_type=command.task_type,
            lifecycle_status=TaskLifecycleStatus.ACTIVE,
            source_module=command.source_module.strip(),
            logical_content_id=command.logical_content_id.strip(),
            parameters=dict(command.parameters),
            requested_terminal_id=command.requested_terminal_id,
            requested_target_device_id=command.requested_target_device_id,
            timeout_seconds=command.timeout_seconds,
            max_retries=command.max_retries,
            record_video=command.record_video,
            created_at=now,
            completed_at=None,
            deleted_at=None,
            row_version=1,
        )
        try:
            with self._uow_factory() as uow:
                existing = uow.tasks.find_by_idempotency_key(task.idempotency_key)
                if existing is not None:
                    return self._idempotent_result(uow, existing, request_hash)
                uow.tasks.add(task)
                uow.flush()
                if schedule is not None:
                    uow.schedules.add(schedule)
                    # The persistence rows intentionally do not carry ORM
                    # relationships.  Flush the FK parent before adding its
                    # children so insertion order is explicit and stable.
                    uow.flush()
                    if schedule_revision is not None:
                        uow.schedule_revisions.add(schedule_revision, revision_times)
                uow.occurrences.add_many(occurrences)
                transitions = [
                    _transition(
                        "task_request",
                        task.task_id,
                        None,
                        task.lifecycle_status.value,
                        "task_created",
                        correlation_id,
                        now,
                    )
                ]
                if schedule is not None:
                    transitions.append(
                        _transition(
                            "task_schedule",
                            schedule.schedule_id,
                            None,
                            schedule.status.value,
                            "schedule_created",
                            correlation_id,
                            now,
                        )
                    )
                uow.transitions.add_many(transitions)
                self._record(
                    uow,
                    event_type="execution.task_created.v1",
                    aggregate_type="task_request",
                    aggregate_id=task.task_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                    payload={
                        "task_id": str(task.task_id),
                        "task_type": task.task_type.value,
                        "occurrence_count": len(occurrences),
                    },
                    action="execution.task.create",
                    reason_code="task_created",
                )
                uow.commit()
        except IntegrityError as exc:
            with self._uow_factory() as uow:
                existing = uow.tasks.find_by_idempotency_key(task.idempotency_key)
                if existing is not None:
                    return self._idempotent_result(uow, existing, request_hash)
            raise ConflictError("task_creation_conflict", "Task creation conflicted") from exc
        return CreatedTask(task, schedule, tuple(occurrences))

    def start_attempt(self, attempt_id: UUID, *, correlation_id: UUID) -> ExecutionAttemptRecord:
        """Internal Stage 3B helper; terminal APIs use TerminalDeliveryService."""
        now = self._now()
        with self._uow_factory() as uow:
            attempt = uow.attempts.get_for_update(attempt_id)
            if attempt is None:
                raise NotFoundError("execution_attempt")
            execution = uow.executions.get_for_update(attempt.execution_id)
            if execution is None:
                raise NotFoundError("execution")
            snapshot = uow.snapshots.get_by_execution(execution.execution_id)
            if snapshot is None:
                raise ConflictError("execution_snapshot_missing", "Execution snapshot is missing")
            pre_start_transitions: list[StateTransitionRecord] = []
            if execution.status is ExecutionStatus.WAITING:
                package = uow.packages.get_by_attempt_for_update(attempt_id)
                if package is None or package.status is not PackageStatus.AVAILABLE:
                    raise ConflictError(
                        "task_package_not_available", "Task package is not available"
                    )
                accepted = uow.packages.set_accepted(package.package_id, package.row_version, now)
                queued_execution = uow.executions.set_status(
                    execution.execution_id,
                    execution.row_version,
                    ExecutionStatus.QUEUED.value,
                    result=None,
                    now=now,
                    timeout_at=None,
                )
                if accepted is None or queued_execution is None:
                    raise ConflictError(
                        "task_package_accept_conflict", "Task package changed concurrently"
                    )
                uow.commands.supersede_pending(attempt_id, acknowledged_at=now)
                pre_start_transitions.append(
                    _transition(
                        "execution",
                        execution.execution_id,
                        execution.status.value,
                        queued_execution.status.value,
                        "internal_package_accepted",
                        correlation_id,
                        now,
                    )
                )
                execution = queued_execution
            require_attempt_transition(attempt.status, AttemptStatus.RUNNING)
            require_execution_transition(execution.status, ExecutionStatus.RUNNING)
            updated_attempt = uow.attempts.set_status(
                attempt.attempt_id,
                attempt.row_version,
                AttemptStatus.RUNNING.value,
                result=None,
                now=now,
                error_code=None,
                retryable=None,
                failure_phase=None,
            )
            updated_execution = uow.executions.set_status(
                execution.execution_id,
                execution.row_version,
                ExecutionStatus.RUNNING.value,
                result=None,
                now=now,
                timeout_at=now + timedelta(seconds=snapshot.timeout_seconds),
            )
            if updated_attempt is None or updated_execution is None:
                raise ConflictError(
                    "attempt_start_conflict", "Execution attempt changed concurrently"
                )
            uow.transitions.add_many(
                [
                    *pre_start_transitions,
                    _transition(
                        "execution_attempt",
                        attempt.attempt_id,
                        attempt.status.value,
                        updated_attempt.status.value,
                        "execution_attempt_started",
                        correlation_id,
                        now,
                    ),
                    _transition(
                        "execution",
                        execution.execution_id,
                        execution.status.value,
                        updated_execution.status.value,
                        "execution_attempt_started",
                        correlation_id,
                        now,
                    ),
                ]
            )
            self._record(
                uow,
                event_type="execution.attempt_started.v1",
                aggregate_type="execution_attempt",
                aggregate_id=attempt.attempt_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "attempt_id": str(attempt.attempt_id),
                    "execution_id": str(execution.execution_id),
                    "attempt_no": attempt.attempt_no,
                },
                action="execution.attempt.start",
                reason_code="execution_attempt_started",
            )
            uow.commit()
        return updated_attempt

    def complete_attempt(
        self,
        attempt_id: UUID,
        *,
        result: ExecutionResult,
        error_code: str | None,
        retryable: bool,
        correlation_id: UUID,
    ) -> AttemptCompletion:
        if error_code is not None and len(error_code) > 100:
            raise InvalidRequestError(
                "invalid_execution_error_code", "Execution error code is too long"
            )
        now = self._now()
        with self._uow_factory() as uow:
            attempt = uow.attempts.get_for_update(attempt_id)
            if attempt is None:
                raise NotFoundError("execution_attempt")
            execution = uow.executions.get_for_update(attempt.execution_id)
            if execution is None:
                raise NotFoundError("execution")
            snapshot = uow.snapshots.get_by_execution(execution.execution_id)
            if snapshot is None:
                raise ConflictError("execution_snapshot_missing", "Execution snapshot is missing")
            schedule = uow.schedules.get_by_task_for_update(execution.task_request_id)
            require_attempt_transition(attempt.status, AttemptStatus.ENDED)
            updated_attempt = uow.attempts.set_status(
                attempt.attempt_id,
                attempt.row_version,
                AttemptStatus.ENDED.value,
                result=result.value,
                now=now,
                error_code=error_code,
                retryable=retryable,
                failure_phase=("runtime" if result is ExecutionResult.FAILURE else None),
            )
            if updated_attempt is None:
                raise ConflictError(
                    "attempt_completion_conflict",
                    "Execution attempt changed concurrently",
                )
            may_retry = (
                result is ExecutionResult.FAILURE
                and retryable
                and attempt.attempt_no <= snapshot.max_retries
                and (schedule is None or schedule.status is ScheduleStatus.ACTIVE)
            )
            retry_attempt: ExecutionAttemptRecord | None = None
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
                retry_attempt = ExecutionAttemptRecord(
                    attempt_id=uuid4(),
                    execution_id=execution.execution_id,
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
                self._add_delivery_for_attempt(
                    uow,
                    attempt=retry_attempt,
                    snapshot=snapshot,
                    correlation_id=correlation_id,
                    now=now,
                )
                execution_reason = "execution_retry_queued"
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
                execution_reason = "execution_completed"
            if updated_execution is None:
                raise ConflictError(
                    "execution_completion_conflict", "Execution changed concurrently"
                )
            transitions = [
                _transition(
                    "execution_attempt",
                    attempt.attempt_id,
                    attempt.status.value,
                    updated_attempt.status.value,
                    "execution_attempt_completed",
                    correlation_id,
                    now,
                ),
                _transition(
                    "execution",
                    execution.execution_id,
                    execution.status.value,
                    updated_execution.status.value,
                    execution_reason,
                    correlation_id,
                    now,
                ),
            ]
            if retry_attempt is not None:
                transitions.append(
                    _transition(
                        "execution_attempt",
                        retry_attempt.attempt_id,
                        None,
                        retry_attempt.status.value,
                        "execution_retry_queued",
                        correlation_id,
                        now,
                    )
                )
            uow.transitions.add_many(transitions)
            if retry_attempt is None:
                settle_task_after_execution(
                    uow,
                    task_id=execution.task_request_id,
                    schedule=schedule,
                    correlation_id=correlation_id,
                    now=now,
                )
            self._record(
                uow,
                event_type=(
                    "execution.retry_queued.v1"
                    if retry_attempt is not None
                    else "execution.completed.v1"
                ),
                aggregate_type="execution",
                aggregate_id=execution.execution_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "execution_id": str(execution.execution_id),
                    "attempt_id": str(attempt.attempt_id),
                    "attempt_no": attempt.attempt_no,
                    "result": result.value,
                    "retry_attempt_id": (str(retry_attempt.attempt_id) if retry_attempt else None),
                },
                action="execution.attempt.complete",
                reason_code=execution_reason,
            )
            uow.commit()
        return AttemptCompletion(updated_attempt, updated_execution, retry_attempt)
