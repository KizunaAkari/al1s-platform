from __future__ import annotations

from datetime import datetime
from uuid import UUID

from al1s.adapters.postgres.scheduling_models import (
    ExecutionAttemptRow,
    ExecutionRow,
    ExecutionSnapshotRow,
    PlanOccurrenceRow,
    StateTransitionRow,
    TaskRequestRow,
    TaskScheduleRevisionRow,
    TaskScheduleRow,
)
from al1s.execution.scheduling_types import (
    AttemptStatus,
    CapabilityRequirements,
    ExecutionAttemptRecord,
    ExecutionRecord,
    ExecutionResult,
    ExecutionSnapshotRecord,
    ExecutionStatus,
    FailurePhase,
    OccurrenceStatus,
    PlanOccurrenceRecord,
    ScheduleRevisionRecord,
    ScheduleStatus,
    StateTransitionRecord,
    TaskLifecycleStatus,
    TaskRequestRecord,
    TaskScheduleRecord,
    TaskType,
)

MAX_PAGE_SIZE = 100
MAX_WORKER_BATCH = 50


def _validate_page(cursor_time: datetime | None, cursor_id: UUID | None, limit: int) -> None:
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    if (cursor_time is None) != (cursor_id is None):
        raise ValueError("cursor time and id must be supplied together")


def _validate_ordinal_page(after_ordinal: int | None, after_id: UUID | None, limit: int) -> None:
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    if (after_ordinal is None) != (after_id is None):
        raise ValueError("cursor ordinal and id must be supplied together")
    if after_ordinal is not None and after_ordinal < 1:
        raise ValueError("cursor ordinal must be positive")


def _task_record(row: TaskRequestRow) -> TaskRequestRecord:
    return TaskRequestRecord(
        task_id=row.id,
        idempotency_key=row.idempotency_key,
        request_hash=row.request_hash,
        name=row.name,
        task_type=TaskType(row.task_type),
        lifecycle_status=TaskLifecycleStatus(row.lifecycle_status),
        source_module=row.source_module,
        logical_content_id=row.logical_content_id,
        parameters=dict(row.parameters),
        requested_terminal_id=row.requested_terminal_id,
        requested_target_device_id=row.requested_target_device_id,
        timeout_seconds=row.timeout_seconds,
        max_retries=row.max_retries,
        record_video=row.record_video,
        created_at=row.created_at,
        completed_at=row.completed_at,
        deleted_at=row.deleted_at,
        row_version=row.row_version,
    )


def _schedule_record(row: TaskScheduleRow) -> TaskScheduleRecord:
    return TaskScheduleRecord(
        schedule_id=row.id,
        task_request_id=row.task_request_id,
        schedule_type=TaskType(row.schedule_type),
        status=ScheduleStatus(row.status),
        total_occurrences=row.total_occurrences,
        repeat_count=row.repeat_count,
        timezone=row.timezone,
        start_date=row.start_date,
        end_date=row.end_date,
        current_revision=row.current_revision,
        paused_at=row.paused_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        row_version=row.row_version,
    )


def _schedule_revision_record(row: TaskScheduleRevisionRow) -> ScheduleRevisionRecord:
    return ScheduleRevisionRecord(
        schedule_revision_id=row.id,
        schedule_id=row.schedule_id,
        revision=row.revision,
        timezone=row.timezone,
        start_date=row.start_date,
        end_date=row.end_date,
        created_at=row.created_at,
    )


def _occurrence_record(row: PlanOccurrenceRow) -> PlanOccurrenceRecord:
    return PlanOccurrenceRecord(
        occurrence_id=row.id,
        task_request_id=row.task_request_id,
        schedule_id=row.schedule_id,
        schedule_revision_id=row.schedule_revision_id,
        ordinal=row.ordinal,
        scheduled_for=row.scheduled_for,
        status=OccurrenceStatus(row.status),
        materialization_owner=row.materialization_owner,
        materialization_expires_at=row.materialization_expires_at,
        created_at=row.created_at,
        settled_at=row.settled_at,
        row_version=row.row_version,
    )


def _execution_record(row: ExecutionRow) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=row.id,
        task_request_id=row.task_request_id,
        occurrence_id=row.occurrence_id,
        terminal_id=row.terminal_id,
        target_device_id=row.target_device_id,
        status=ExecutionStatus(row.status),
        result=ExecutionResult(row.result) if row.result else None,
        timeout_at=row.timeout_at,
        cancel_requested_at=row.cancel_requested_at,
        cancel_reason=row.cancel_reason,
        cancel_deadline_at=row.cancel_deadline_at,
        cancel_delivery_delayed=row.cancel_delivery_delayed,
        cancel_delivery_note=row.cancel_delivery_note,
        created_at=row.created_at,
        queued_at=row.queued_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
        row_version=row.row_version,
    )


def _attempt_record(row: ExecutionAttemptRow) -> ExecutionAttemptRecord:
    return ExecutionAttemptRecord(
        attempt_id=row.id,
        execution_id=row.execution_id,
        attempt_no=row.attempt_no,
        status=AttemptStatus(row.status),
        result=ExecutionResult(row.result) if row.result else None,
        available_at=row.available_at,
        enqueued_at=row.enqueued_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
        error_code=row.error_code,
        retryable=row.retryable,
        failure_phase=FailurePhase(row.failure_phase) if row.failure_phase else None,
        lease_id=row.lease_id,
        row_version=row.row_version,
    )


def _snapshot_record(row: ExecutionSnapshotRow) -> ExecutionSnapshotRecord:
    raw = dict(row.capability_requirements)
    requirements = CapabilityRequirements(
        schema_version=int(raw.get("schema_version", 1)),
        min_protocol_version=int(raw.get("min_protocol_version", 1)),
        architectures=tuple(raw.get("architectures", [])),
        min_memory_bytes=int(raw.get("min_memory_bytes", 0)),
        min_storage_bytes=int(raw.get("min_storage_bytes", 0)),
        accelerator_type=raw.get("accelerator_type"),
        provider_keys=tuple(raw.get("provider_keys", [])),
        requires_target_device=bool(raw.get("requires_target_device", False)),
    )
    return ExecutionSnapshotRecord(
        snapshot_id=row.id,
        execution_id=row.execution_id,
        source_module=row.source_module,
        logical_content_id=row.logical_content_id,
        revision_id=row.revision_id,
        schema_version=row.schema_version,
        manifest_hash=row.manifest_hash,
        manifest=dict(row.manifest),
        parameters=dict(row.parameters),
        capability_requirements=requirements,
        terminal_id=row.terminal_id,
        target_device_id=row.target_device_id,
        timeout_seconds=row.timeout_seconds,
        max_retries=row.max_retries,
        record_video=row.record_video,
        created_at=row.created_at,
    )


def _transition_record(row: StateTransitionRow) -> StateTransitionRecord:
    return StateTransitionRecord(
        transition_id=row.id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        from_status=row.from_status,
        to_status=row.to_status,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        reason_code=row.reason_code,
        correlation_id=row.correlation_id,
        occurred_at=row.occurred_at,
    )
