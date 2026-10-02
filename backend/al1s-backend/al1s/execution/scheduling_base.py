from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from al1s.execution.definitions import ExecutionDefinitionRegistry
from al1s.execution.errors import ConflictError, InvalidRequestError, NotFoundError
from al1s.execution.package_builder import create_delivery_records
from al1s.execution.schedule_revision_service import ScheduleRevisionService
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_types import (
    CreatedTask,
    CreateTaskCommand,
    ExecutionAttemptRecord,
    ExecutionSnapshotRecord,
    OriginalSnapshotRetry,
    RetryOriginalSnapshotCommand,
    ScheduleRevisionResult,
    TaskCancellation,
    TaskRequestRecord,
    TaskScheduleRecord,
    TaskType,
    UpdateTimedScheduleCommand,
)
from al1s.execution.snapshot_retry_service import SnapshotRetryService
from al1s.execution.task_lifecycle import TaskLifecycleOperations
from al1s.kernel.types import NewAuditEntry, NewOutboxEvent

SchedulingUowFactory = Callable[[], SchedulingUnitOfWork]


class SchedulingContext:
    def __init__(
        self,
        uow_factory: SchedulingUowFactory,
        definitions: ExecutionDefinitionRegistry,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._definitions = definitions
        self._now = now or (lambda: datetime.now(UTC))
        self._task_lifecycle = TaskLifecycleOperations(uow_factory, self._now)
        self._schedule_revisions = ScheduleRevisionService(uow_factory, self._now)
        self._snapshot_retries = SnapshotRetryService(uow_factory, self._now)

    def revise_timed_schedule(
        self,
        schedule_id: UUID,
        command: UpdateTimedScheduleCommand,
        *,
        correlation_id: UUID,
    ) -> ScheduleRevisionResult:
        return self._schedule_revisions.revise(schedule_id, command, correlation_id=correlation_id)

    def retry_original_snapshot(
        self,
        source_execution_id: UUID,
        command: RetryOriginalSnapshotCommand,
        *,
        correlation_id: UUID,
    ) -> OriginalSnapshotRetry:
        return self._snapshot_retries.retry(
            source_execution_id, command, correlation_id=correlation_id
        )

    def cancel_single_task(
        self, task_id: UUID, *, expected_version: int, correlation_id: UUID
    ) -> TaskCancellation:
        return self._task_lifecycle.cancel_single_task(
            task_id,
            expected_version=expected_version,
            correlation_id=correlation_id,
        )

    def delete_task_history(
        self, task_id: UUID, *, expected_version: int, correlation_id: UUID
    ) -> None:
        self._task_lifecycle.delete_history(
            task_id,
            expected_version=expected_version,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _idempotent_result(
        uow: SchedulingUnitOfWork,
        existing: TaskRequestRecord,
        request_hash: str,
    ) -> CreatedTask:
        if existing.request_hash != request_hash:
            raise ConflictError(
                "idempotency_key_reused",
                "Idempotency key was already used for a different task request",
            )
        result = uow.tasks.get_created_task(existing.task_id)
        if result is None:
            raise ConflictError("task_state_incomplete", "Existing task request is incomplete")
        return result

    @staticmethod
    def _validate_command(command: CreateTaskCommand) -> None:
        if not command.idempotency_key.strip() or len(command.idempotency_key) > 128:
            raise InvalidRequestError(
                "invalid_idempotency_key", "Idempotency key must contain 1 to 128 characters"
            )
        if not command.name.strip() or len(command.name) > 160:
            raise InvalidRequestError("invalid_task_name", "Task name is invalid")
        if not command.source_module.strip() or not command.logical_content_id.strip():
            raise InvalidRequestError(
                "invalid_execution_reference", "Execution definition reference is required"
            )
        if not 1 <= command.timeout_seconds <= 4 * 60 * 60:
            raise InvalidRequestError(
                "invalid_task_timeout", "Task timeout must be between 1 second and 4 hours"
            )
        if not 0 <= command.max_retries <= 100:
            raise InvalidRequestError(
                "invalid_retry_count", "Retry count must be between 0 and 100"
            )
        valid_shape = (
            (
                command.task_type is TaskType.SINGLE
                and command.loop is None
                and command.timed is None
            )
            or (
                command.task_type is TaskType.LOOP
                and command.loop is not None
                and command.timed is None
            )
            or (
                command.task_type is TaskType.TIMED
                and command.timed is not None
                and command.loop is None
            )
        )
        if not valid_shape:
            raise InvalidRequestError(
                "invalid_task_schedule", "Task type and schedule definition do not match"
            )

    @staticmethod
    def _require_schedule(uow: SchedulingUnitOfWork, schedule_id: UUID) -> TaskScheduleRecord:
        schedule = uow.schedules.get_for_update(schedule_id)
        if schedule is None:
            raise NotFoundError("task_schedule")
        return schedule

    @staticmethod
    def _stale_schedule() -> None:
        raise ConflictError("stale_schedule_version", "Task schedule changed concurrently")

    def _record_schedule_change(
        self,
        uow: SchedulingUnitOfWork,
        schedule: TaskScheduleRecord,
        action: str,
        correlation_id: UUID,
        now: datetime,
        affected_occurrences: int,
    ) -> None:
        self._record(
            uow,
            event_type=f"execution.schedule_{action}.v1",
            aggregate_type="task_schedule",
            aggregate_id=schedule.schedule_id,
            correlation_id=correlation_id,
            occurred_at=now,
            payload={
                "schedule_id": str(schedule.schedule_id),
                "task_id": str(schedule.task_request_id),
                "status": schedule.status.value,
                "affected_occurrences": affected_occurrences,
            },
            action=f"execution.schedule.{action}",
            reason_code=f"schedule_{action}",
        )

    @staticmethod
    def _record(
        uow: SchedulingUnitOfWork,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
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
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                correlation_id=correlation_id,
                occurred_at=occurred_at,
                payload=dict(payload),
            )
        )
        uow.audit.add(
            NewAuditEntry(
                audit_id=uuid4(),
                actor_type="operator" if action != "execution.materialize" else "system",
                actor_id=None,
                action=action,
                target_type=aggregate_type,
                target_id=aggregate_id,
                correlation_id=correlation_id,
                details={"reason_code": reason_code, **payload},
                summary=reason_code,
            )
        )

    def _add_delivery_for_attempt(
        self,
        uow: SchedulingUnitOfWork,
        *,
        attempt: ExecutionAttemptRecord,
        snapshot: ExecutionSnapshotRecord,
        correlation_id: UUID,
        now: datetime,
    ) -> None:
        uow.flush()
        resources = uow.snapshots.list_package_resources(snapshot.snapshot_id)
        package, command = create_delivery_records(
            attempt=attempt,
            snapshot=snapshot,
            resources=resources,
            created_at=now,
        )
        uow.packages.add(package)
        uow.flush()
        uow.commands.add(command)
        self._record(
            uow,
            event_type="task_package.available.v1",
            aggregate_type="task_package",
            aggregate_id=package.package_id,
            correlation_id=correlation_id,
            occurred_at=now,
            payload={
                "terminal_id": str(package.terminal_id),
                "command_id": str(command.command_id),
                "package_id": str(package.package_id),
                "attempt_id": str(attempt.attempt_id),
            },
            action="execution.package.publish",
            reason_code="task_package_available",
        )
