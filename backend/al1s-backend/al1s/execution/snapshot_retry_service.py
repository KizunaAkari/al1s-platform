from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from al1s.execution.errors import ConflictError, InvalidRequestError, NotFoundError
from al1s.execution.package_builder import create_delivery_records
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_types import (
    AttemptStatus,
    ExecutionAttemptRecord,
    ExecutionRecord,
    ExecutionResult,
    ExecutionSnapshotRecord,
    ExecutionStatus,
    OccurrenceStatus,
    OriginalSnapshotRetry,
    PlanOccurrenceRecord,
    RetryOriginalSnapshotCommand,
    StateTransitionRecord,
    TaskLifecycleStatus,
    TaskRequestRecord,
    TaskRetryOriginRecord,
    TaskType,
)
from al1s.kernel.types import NewAuditEntry, NewOutboxEvent


class SnapshotRetryService:
    """Creates a new single task from an ended failure's immutable snapshot."""

    def __init__(
        self,
        uow_factory: Callable[[], SchedulingUnitOfWork],
        now: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now

    def retry(
        self,
        source_execution_id: UUID,
        command: RetryOriginalSnapshotCommand,
        *,
        correlation_id: UUID,
    ) -> OriginalSnapshotRetry:
        idempotency_key = command.idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 128:
            raise InvalidRequestError(
                "invalid_idempotency_key", "Idempotency key must contain 1 to 128 characters"
            )
        requested_name = command.name.strip() if command.name is not None else None
        if requested_name is not None and (not requested_name or len(requested_name) > 160):
            raise InvalidRequestError("invalid_task_name", "Task name is invalid")

        with self._uow_factory() as uow:
            source = uow.executions.get(source_execution_id)
            if source is None:
                raise NotFoundError("execution")
            source_task = uow.tasks.get(source.task_request_id)
            if source_task is None:
                raise NotFoundError("task")
            _require_generic_retry_source(source_task.source_module)
            task_name = requested_name or _default_name(source_task.name)
            request_hash = _request_hash(source_execution_id, task_name)
            existing = uow.tasks.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return _idempotent_result(uow, existing, request_hash, source_execution_id)

        now = self._now()
        try:
            with self._uow_factory() as uow:
                existing = uow.tasks.find_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return _idempotent_result(uow, existing, request_hash, source_execution_id)
                source = uow.executions.get_for_update(source_execution_id)
                if source is None:
                    raise NotFoundError("execution")
                if (
                    source.status is not ExecutionStatus.ENDED
                    or source.result is not ExecutionResult.FAILURE
                ):
                    raise ConflictError(
                        "snapshot_retry_requires_failed_execution",
                        "Original snapshot retry requires an ended failed execution",
                    )
                source_snapshot = uow.snapshots.get_by_execution(source_execution_id)
                if source_snapshot is None:
                    raise ConflictError(
                        "execution_snapshot_missing", "Execution snapshot is missing"
                    )
                _require_generic_retry_source(source_snapshot.source_module)
                source_blobs = uow.snapshots.list_blobs(source_snapshot.snapshot_id)
                task = _task(
                    idempotency_key,
                    request_hash,
                    task_name,
                    source_snapshot,
                    now,
                )
                occurrence = _occurrence(task.task_id, now)
                execution = _execution(task.task_id, occurrence.occurrence_id, source_snapshot, now)
                attempt = _attempt(execution.execution_id, now)
                snapshot = _snapshot(execution.execution_id, source_snapshot, now)
                origin = TaskRetryOriginRecord(
                    task_request_id=task.task_id,
                    source_execution_id=source_execution_id,
                    source_snapshot_id=source_snapshot.snapshot_id,
                    created_at=now,
                )
                uow.tasks.add(task)
                uow.flush()
                uow.occurrences.add_many([occurrence])
                uow.flush()
                uow.executions.add(execution)
                uow.flush()
                uow.attempts.add(attempt)
                uow.snapshots.add(snapshot, source_blobs)
                uow.flush()
                package, delivery_command = create_delivery_records(
                    attempt=attempt,
                    snapshot=snapshot,
                    resources=uow.snapshots.list_package_resources(snapshot.snapshot_id),
                    created_at=now,
                )
                uow.packages.add(package)
                uow.flush()
                uow.commands.add(delivery_command)
                uow.retry_origins.add(origin)
                uow.transitions.add_many(
                    _transitions(task, occurrence, execution, attempt, correlation_id, now)
                )
                _record(uow, task, execution, origin, correlation_id, now)
                uow.outbox.add(
                    NewOutboxEvent(
                        event_id=uuid4(),
                        event_type="task_package.available.v1",
                        schema_version=1,
                        aggregate_type="task_package",
                        aggregate_id=package.package_id,
                        correlation_id=correlation_id,
                        occurred_at=now,
                        payload={
                            "terminal_id": str(package.terminal_id),
                            "command_id": str(delivery_command.command_id),
                            "package_id": str(package.package_id),
                            "attempt_id": str(attempt.attempt_id),
                        },
                    )
                )
                uow.commit()
        except IntegrityError as exc:
            with self._uow_factory() as uow:
                existing = uow.tasks.find_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return _idempotent_result(uow, existing, request_hash, source_execution_id)
            raise ConflictError(
                "snapshot_retry_creation_conflict", "Original snapshot retry conflicted"
            ) from exc
        return OriginalSnapshotRetry(task, occurrence, execution, attempt, snapshot, origin)


def _default_name(source_name: str) -> str:
    suffix = " (原快照重试)"
    return f"{source_name[: 160 - len(suffix)]}{suffix}"


def _request_hash(source_execution_id: UUID, name: str) -> str:
    encoded = json.dumps(
        {
            "operation": "retry_original_snapshot",
            "source_execution_id": str(source_execution_id),
            "name": name,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _task(
    idempotency_key: str,
    request_hash: str,
    name: str,
    source: ExecutionSnapshotRecord,
    now: datetime,
) -> TaskRequestRecord:
    return TaskRequestRecord(
        task_id=uuid4(),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        name=name,
        task_type=TaskType.SINGLE,
        lifecycle_status=TaskLifecycleStatus.ACTIVE,
        source_module=source.source_module,
        logical_content_id=source.logical_content_id,
        parameters=dict(source.parameters),
        requested_terminal_id=source.terminal_id,
        requested_target_device_id=source.target_device_id,
        timeout_seconds=source.timeout_seconds,
        max_retries=source.max_retries,
        record_video=source.record_video,
        created_at=now,
        completed_at=None,
        deleted_at=None,
        row_version=1,
    )


def _occurrence(task_id: UUID, now: datetime) -> PlanOccurrenceRecord:
    return PlanOccurrenceRecord(
        occurrence_id=uuid4(),
        task_request_id=task_id,
        schedule_id=None,
        schedule_revision_id=None,
        ordinal=1,
        scheduled_for=now,
        status=OccurrenceStatus.MATERIALIZED,
        materialization_owner=None,
        materialization_expires_at=None,
        created_at=now,
        settled_at=None,
        row_version=1,
    )


def _execution(
    task_id: UUID,
    occurrence_id: UUID,
    source: ExecutionSnapshotRecord,
    now: datetime,
) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=uuid4(),
        task_request_id=task_id,
        occurrence_id=occurrence_id,
        terminal_id=source.terminal_id,
        target_device_id=source.target_device_id,
        status=ExecutionStatus.WAITING,
        result=None,
        timeout_at=None,
        cancel_requested_at=None,
        cancel_reason=None,
        cancel_deadline_at=None,
        cancel_delivery_delayed=False,
        cancel_delivery_note=None,
        created_at=now,
        queued_at=now,
        started_at=None,
        ended_at=None,
        row_version=1,
    )


def _attempt(execution_id: UUID, now: datetime) -> ExecutionAttemptRecord:
    return ExecutionAttemptRecord(
        attempt_id=uuid4(),
        execution_id=execution_id,
        attempt_no=1,
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


def _snapshot(
    execution_id: UUID,
    source: ExecutionSnapshotRecord,
    now: datetime,
) -> ExecutionSnapshotRecord:
    return ExecutionSnapshotRecord(
        snapshot_id=uuid4(),
        execution_id=execution_id,
        source_module=source.source_module,
        logical_content_id=source.logical_content_id,
        revision_id=source.revision_id,
        schema_version=source.schema_version,
        manifest_hash=source.manifest_hash,
        manifest=dict(source.manifest),
        parameters=dict(source.parameters),
        capability_requirements=source.capability_requirements,
        terminal_id=source.terminal_id,
        target_device_id=source.target_device_id,
        timeout_seconds=source.timeout_seconds,
        max_retries=source.max_retries,
        record_video=source.record_video,
        created_at=now,
    )


def _transitions(
    task: TaskRequestRecord,
    occurrence: PlanOccurrenceRecord,
    execution: ExecutionRecord,
    attempt: ExecutionAttemptRecord,
    correlation_id: UUID,
    now: datetime,
) -> list[StateTransitionRecord]:
    entries = (
        ("task_request", task.task_id, task.lifecycle_status.value),
        ("plan_occurrence", occurrence.occurrence_id, occurrence.status.value),
        ("execution", execution.execution_id, execution.status.value),
        ("execution_attempt", attempt.attempt_id, attempt.status.value),
    )
    return [
        StateTransitionRecord(
            transition_id=uuid4(),
            aggregate_type=kind,
            aggregate_id=identifier,
            from_status=None,
            to_status=status,
            actor_type="operator",
            actor_id=None,
            reason_code="original_snapshot_retry_created",
            correlation_id=correlation_id,
            occurred_at=now,
        )
        for kind, identifier, status in entries
    ]


def _record(
    uow: SchedulingUnitOfWork,
    task: TaskRequestRecord,
    execution: ExecutionRecord,
    origin: TaskRetryOriginRecord,
    correlation_id: UUID,
    now: datetime,
) -> None:
    payload: dict[str, object] = {
        "task_id": str(task.task_id),
        "execution_id": str(execution.execution_id),
        "source_execution_id": str(origin.source_execution_id),
        "source_snapshot_id": str(origin.source_snapshot_id),
    }
    uow.outbox.add(
        NewOutboxEvent(
            event_id=uuid4(),
            event_type="execution.original_snapshot_retry_created.v1",
            schema_version=1,
            aggregate_type="task_request",
            aggregate_id=task.task_id,
            correlation_id=correlation_id,
            occurred_at=now,
            payload=payload,
        )
    )
    uow.audit.add(
        NewAuditEntry(
            audit_id=uuid4(),
            actor_type="operator",
            actor_id=None,
            action="execution.original_snapshot_retry.create",
            target_type="task_request",
            target_id=task.task_id,
            correlation_id=correlation_id,
            details={"reason_code": "original_snapshot_retry_created", **payload},
            summary="original_snapshot_retry_created",
        )
    )


def _idempotent_result(
    uow: SchedulingUnitOfWork,
    task: TaskRequestRecord,
    request_hash: str,
    source_execution_id: UUID,
) -> OriginalSnapshotRetry:
    if task.request_hash != request_hash:
        raise ConflictError(
            "idempotency_key_reused",
            "Idempotency key was already used for a different task request",
        )
    origin = uow.retry_origins.get_by_task(task.task_id)
    execution = uow.executions.get_by_task(task.task_id)
    created = uow.tasks.get_created_task(task.task_id)
    if (
        origin is None
        or origin.source_execution_id != source_execution_id
        or execution is None
        or created is None
        or len(created.occurrences) != 1
    ):
        raise ConflictError(
            "snapshot_retry_state_incomplete", "Existing original snapshot retry is incomplete"
        )
    snapshot = uow.snapshots.get_by_execution(execution.execution_id)
    attempts = uow.attempts.list_by_execution(execution.execution_id, limit=2)
    if snapshot is None or not attempts:
        raise ConflictError(
            "snapshot_retry_state_incomplete", "Existing original snapshot retry is incomplete"
        )
    return OriginalSnapshotRetry(
        task,
        created.occurrences[0],
        execution,
        attempts[0],
        snapshot,
        origin,
    )


def _require_generic_retry_source(source_module: str) -> None:
    if source_module in {"lineup", "lineup_batch"}:
        raise ConflictError(
            "lineup_retry_requires_workspace", "阵容任务请通过阵容工作区重试失败子集"
        )
