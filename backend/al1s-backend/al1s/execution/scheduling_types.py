from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any
from uuid import UUID

from al1s.execution.types import ActionAvailability, CapabilityManifest


class TaskType(StrEnum):
    SINGLE = "single"
    BATCH = "batch"
    LOOP = "loop"
    TIMED = "timed"


class TaskLifecycleStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TERMINATED = "terminated"


class ScheduleStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    TERMINATING = "terminating"
    TERMINATED = "terminated"
    COMPLETED = "completed"


class OccurrenceStatus(StrEnum):
    PLANNED = "planned"
    MATERIALIZED = "materialized"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ExecutionStatus(StrEnum):
    WAITING = "waiting"
    QUEUED = "queued"
    RUNNING = "running"
    ENDED = "ended"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class AttemptStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    ENDED = "ended"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ExecutionResult(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class FailurePhase(StrEnum):
    DELIVERY = "delivery"
    RUNTIME = "runtime"
    CLEANUP = "cleanup"
    PLATFORM = "platform"


@dataclass(frozen=True, slots=True)
class CapabilityRequirements:
    schema_version: int = 1
    min_protocol_version: int = 1
    architectures: tuple[str, ...] = ()
    min_memory_bytes: int = 0
    min_storage_bytes: int = 0
    accelerator_type: str | None = None
    provider_keys: tuple[str, ...] = ()
    requires_target_device: bool = False


@dataclass(frozen=True, slots=True)
class SnapshotBlobReference:
    blob_id: UUID
    resource_key: str
    role: str


@dataclass(frozen=True, slots=True)
class ResolvedExecutionDefinition:
    revision_id: str
    schema_version: int
    manifest_hash: str
    manifest: dict[str, Any]
    capability_requirements: CapabilityRequirements
    blobs: tuple[SnapshotBlobReference, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskRequestRecord:
    task_id: UUID
    idempotency_key: str
    request_hash: str
    name: str
    task_type: TaskType
    lifecycle_status: TaskLifecycleStatus
    source_module: str
    logical_content_id: str
    parameters: dict[str, Any]
    requested_terminal_id: UUID | None
    requested_target_device_id: UUID | None
    timeout_seconds: int
    max_retries: int
    record_video: bool
    created_at: datetime
    completed_at: datetime | None
    deleted_at: datetime | None
    row_version: int


@dataclass(frozen=True, slots=True)
class TaskScheduleRecord:
    schedule_id: UUID
    task_request_id: UUID
    schedule_type: TaskType
    status: ScheduleStatus
    total_occurrences: int
    repeat_count: int | None
    timezone: str | None
    start_date: date | None
    end_date: date | None
    current_revision: int | None
    paused_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    row_version: int


@dataclass(frozen=True, slots=True)
class ActiveContentScheduleImpact:
    task_id: UUID
    schedule_id: UUID
    task_name: str
    task_type: TaskType
    schedule_status: ScheduleStatus
    logical_content_id: str
    future_occurrence_count: int
    first_scheduled_for: datetime | None
    last_scheduled_for: datetime | None
    task_row_version: int
    schedule_row_version: int


@dataclass(frozen=True, slots=True)
class ScheduleRevisionRecord:
    schedule_revision_id: UUID
    schedule_id: UUID
    revision: int
    timezone: str
    start_date: date
    end_date: date
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ScheduleRevisionTimeRecord:
    schedule_revision_time_id: UUID
    schedule_revision_id: UUID
    ordinal: int
    local_time: time


@dataclass(frozen=True, slots=True)
class PlanOccurrenceRecord:
    occurrence_id: UUID
    task_request_id: UUID
    schedule_id: UUID | None
    schedule_revision_id: UUID | None
    ordinal: int
    scheduled_for: datetime | None
    status: OccurrenceStatus
    materialization_owner: UUID | None
    materialization_expires_at: datetime | None
    created_at: datetime
    settled_at: datetime | None
    row_version: int


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    execution_id: UUID
    task_request_id: UUID
    occurrence_id: UUID
    terminal_id: UUID
    target_device_id: UUID | None
    status: ExecutionStatus
    result: ExecutionResult | None
    timeout_at: datetime | None
    cancel_requested_at: datetime | None
    cancel_reason: str | None
    cancel_deadline_at: datetime | None
    cancel_delivery_delayed: bool
    cancel_delivery_note: str | None
    created_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    row_version: int


@dataclass(frozen=True, slots=True)
class ExecutionAttemptRecord:
    attempt_id: UUID
    execution_id: UUID
    attempt_no: int
    status: AttemptStatus
    result: ExecutionResult | None
    available_at: datetime
    enqueued_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    error_code: str | None
    retryable: bool | None
    failure_phase: FailurePhase | None
    lease_id: UUID | None
    row_version: int


@dataclass(frozen=True, slots=True)
class RuntimeTimeoutCandidate:
    execution: ExecutionRecord
    attempt_id: UUID
    package_id: UUID
    protocol_version: int


@dataclass(frozen=True, slots=True)
class CleanupDeadlineCandidate:
    execution: ExecutionRecord
    attempt: ExecutionAttemptRecord
    lease_id: UUID | None
    lease_version: int | None


@dataclass(frozen=True, slots=True)
class ExecutionSnapshotRecord:
    snapshot_id: UUID
    execution_id: UUID
    source_module: str
    logical_content_id: str
    revision_id: str
    schema_version: int
    manifest_hash: str
    manifest: dict[str, Any]
    parameters: dict[str, Any]
    capability_requirements: CapabilityRequirements
    terminal_id: UUID
    target_device_id: UUID | None
    timeout_seconds: int
    max_retries: int
    record_video: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StateTransitionRecord:
    transition_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    from_status: str | None
    to_status: str
    actor_type: str
    actor_id: UUID | None
    reason_code: str
    correlation_id: UUID
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class CreatedTask:
    task: TaskRequestRecord
    schedule: TaskScheduleRecord | None
    occurrences: tuple[PlanOccurrenceRecord, ...]


@dataclass(frozen=True, slots=True)
class MaterializedExecution:
    occurrence: PlanOccurrenceRecord
    execution: ExecutionRecord
    attempt: ExecutionAttemptRecord
    snapshot: ExecutionSnapshotRecord


@dataclass(frozen=True, slots=True)
class AttemptCompletion:
    attempt: ExecutionAttemptRecord
    execution: ExecutionRecord
    retry_attempt: ExecutionAttemptRecord | None


@dataclass(frozen=True, slots=True)
class LoopScheduleSpec:
    repeat_count: int


@dataclass(frozen=True, slots=True)
class TimedScheduleSpec:
    timezone: str
    start_date: date
    end_date: date
    daily_times: tuple[time, ...]


@dataclass(frozen=True, slots=True)
class CreateTaskCommand:
    idempotency_key: str
    name: str
    task_type: TaskType
    source_module: str
    logical_content_id: str
    parameters: dict[str, Any]
    requested_terminal_id: UUID | None
    requested_target_device_id: UUID | None
    timeout_seconds: int
    max_retries: int
    record_video: bool
    loop: LoopScheduleSpec | None = None
    timed: TimedScheduleSpec | None = None


@dataclass(frozen=True, slots=True)
class UpdateTimedScheduleCommand:
    expected_version: int
    start_date: date
    end_date: date
    daily_times: tuple[time, ...]


@dataclass(frozen=True, slots=True)
class RetryOriginalSnapshotCommand:
    idempotency_key: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class MaterializationCandidate:
    occurrence: PlanOccurrenceRecord
    task: TaskRequestRecord


@dataclass(frozen=True, slots=True)
class PreparedMaterialization:
    occurrence_id: UUID
    task: TaskRequestRecord
    definition: ResolvedExecutionDefinition
    terminal_id: UUID
    target_device_id: UUID | None


@dataclass(frozen=True, slots=True)
class EligibleExecutionResource:
    terminal_id: UUID
    capability: CapabilityManifest
    managed_target_device_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class BatchHistorySummary:
    image_count: int
    execution_status: ExecutionStatus
    result: ExecutionResult | None


@dataclass(frozen=True, slots=True)
class TaskHistorySummary:
    task_id: UUID
    name: str
    task_type: TaskType
    lifecycle_status: TaskLifecycleStatus
    schedule_status: ScheduleStatus | None
    latest_execution_id: UUID | None
    latest_execution_status: ExecutionStatus | None
    latest_result: ExecutionResult | None
    record_video: bool
    created_at: datetime
    completed_at: datetime | None
    row_version: int
    source_module: str = ""
    logical_content_id: str = ""
    batch_summary: BatchHistorySummary | None = None


@dataclass(frozen=True, slots=True)
class ActiveScheduleSummary:
    task_id: UUID
    schedule_id: UUID
    name: str
    schedule_type: TaskType
    status: ScheduleStatus
    timezone: str | None
    start_date: date | None
    end_date: date | None
    daily_times: tuple[time, ...]
    current_revision: int | None
    total_occurrences: int
    settled_occurrences: int
    planned_occurrences: int
    materialized_occurrences: int
    skipped_occurrences: int
    cancelled_occurrences: int
    current_occurrence_ordinal: int | None
    next_occurrence_ordinal: int | None
    row_version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskHistoryManagementSummary:
    task: TaskHistorySummary
    cancel: ActionAvailability
    delete: ActionAvailability


@dataclass(frozen=True, slots=True)
class ActiveScheduleManagementSummary:
    schedule: ActiveScheduleSummary
    pause: ActionAvailability
    resume: ActionAvailability
    terminate: ActionAvailability
    revise: ActionAvailability


@dataclass(frozen=True, slots=True)
class OccurrenceSummary:
    occurrence: PlanOccurrenceRecord
    display_ordinal: int | None
    schedule_revision: int | None
    execution_id: UUID | None
    execution_status: ExecutionStatus | None
    execution_result: ExecutionResult | None


@dataclass(frozen=True, slots=True)
class ExecutionDetails:
    execution: ExecutionRecord
    attempts: tuple[ExecutionAttemptRecord, ...]
    snapshot: ExecutionSnapshotRecord
    transitions: tuple[StateTransitionRecord, ...]


@dataclass(frozen=True, slots=True)
class TaskCancellation:
    task: TaskRequestRecord
    execution: ExecutionRecord | None
    pending_terminal_ack: bool


@dataclass(frozen=True, slots=True)
class ScheduleRevisionResult:
    schedule: TaskScheduleRecord
    revision: ScheduleRevisionRecord
    retained_occurrences: int
    cancelled_occurrences: int
    added_occurrences: tuple[PlanOccurrenceRecord, ...]
    changed: bool


@dataclass(frozen=True, slots=True)
class TaskRetryOriginRecord:
    task_request_id: UUID
    source_execution_id: UUID
    source_snapshot_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OriginalSnapshotRetry:
    task: TaskRequestRecord
    occurrence: PlanOccurrenceRecord
    execution: ExecutionRecord
    attempt: ExecutionAttemptRecord
    snapshot: ExecutionSnapshotRecord
    origin: TaskRetryOriginRecord
