from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from types import TracebackType
from typing import Protocol
from uuid import UUID

from al1s.execution.delivery_ports import (
    CommandAcknowledgementRepository,
    OfflineStartPermitRepository,
    TaskPackageRepository,
    TerminalCommandRepository,
    TerminalReportRepository,
)
from al1s.execution.delivery_types import PackageResource
from al1s.execution.ports import ExecutionLeaseRepository
from al1s.execution.scheduling_types import (
    ActiveContentScheduleImpact,
    ActiveScheduleSummary,
    CleanupDeadlineCandidate,
    CreatedTask,
    EligibleExecutionResource,
    ExecutionAttemptRecord,
    ExecutionRecord,
    ExecutionSnapshotRecord,
    MaterializationCandidate,
    OccurrenceSummary,
    PlanOccurrenceRecord,
    RuntimeTimeoutCandidate,
    ScheduleRevisionRecord,
    ScheduleRevisionTimeRecord,
    SnapshotBlobReference,
    StateTransitionRecord,
    TaskHistorySummary,
    TaskRequestRecord,
    TaskRetryOriginRecord,
    TaskScheduleRecord,
)
from al1s.kernel.ports import AuditRepository, OutboxRepository


class TaskRepository(Protocol):
    def add(self, task: TaskRequestRecord) -> None: ...

    def find_by_idempotency_key(self, key: str) -> TaskRequestRecord | None: ...

    def get(self, task_id: UUID, *, for_update: bool = False) -> TaskRequestRecord | None: ...

    def get_for_reconciliation(self, task_id: UUID) -> TaskRequestRecord | None: ...

    def get_created_task(self, task_id: UUID) -> CreatedTask | None: ...

    def set_lifecycle(
        self,
        task_id: UUID,
        expected_version: int,
        status: str,
        completed_at: datetime | None,
    ) -> TaskRequestRecord | None: ...

    def list_history(
        self,
        *,
        before_created_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[TaskHistorySummary]: ...

    def soft_delete(
        self, task_id: UUID, expected_version: int, deleted_at: datetime
    ) -> TaskRequestRecord | None: ...


class ScheduleRepository(Protocol):
    def add(self, schedule: TaskScheduleRecord) -> None: ...

    def get_for_update(self, schedule_id: UUID) -> TaskScheduleRecord | None: ...

    def get(self, schedule_id: UUID) -> TaskScheduleRecord | None: ...

    def get_by_task_for_update(self, task_id: UUID) -> TaskScheduleRecord | None: ...

    def set_status(
        self,
        schedule_id: UUID,
        expected_version: int,
        status: str,
        now: datetime,
        *,
        paused_at: datetime | None,
        completed_at: datetime | None,
    ) -> TaskScheduleRecord | None: ...

    def set_timed_definition(
        self,
        schedule_id: UUID,
        expected_version: int,
        *,
        total_occurrences: int,
        start_date: date,
        end_date: date,
        current_revision: int,
        now: datetime,
    ) -> TaskScheduleRecord | None: ...

    def list_active(
        self,
        *,
        cursor_updated_at: datetime | None,
        cursor_id: UUID | None,
        limit: int,
        descending: bool,
    ) -> list[ActiveScheduleSummary]: ...


class ContentScheduleImpactRepository(Protocol):
    def list_active_future_impacts(
        self,
        *,
        source_module: str,
        logical_content_ids: Sequence[str],
    ) -> list[ActiveContentScheduleImpact]: ...


class ScheduleRevisionRepository(Protocol):
    def add(
        self,
        revision: ScheduleRevisionRecord,
        times: Sequence[ScheduleRevisionTimeRecord],
    ) -> None: ...

    def get_current(self, schedule: TaskScheduleRecord) -> ScheduleRevisionRecord | None: ...

    def list_times(self, schedule_revision_id: UUID) -> tuple[time, ...]: ...


class OccurrenceRepository(Protocol):
    def add_many(self, occurrences: Sequence[PlanOccurrenceRecord]) -> None: ...

    def claim_due(
        self,
        *,
        worker_id: UUID,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
    ) -> list[MaterializationCandidate]: ...

    def mark_materialized(
        self,
        occurrence_id: UUID,
        expected_owner: UUID,
        settled_at: datetime,
    ) -> PlanOccurrenceRecord | None: ...

    def skip_overdue_timed(
        self, schedule_id: UUID, now: datetime
    ) -> list[PlanOccurrenceRecord]: ...

    def cancel_planned(self, schedule_id: UUID, now: datetime) -> list[PlanOccurrenceRecord]: ...

    def cancel_planned_for_task(
        self, task_id: UUID, now: datetime
    ) -> list[PlanOccurrenceRecord]: ...

    def list_for_schedule_update(self, schedule_id: UUID) -> list[PlanOccurrenceRecord]: ...

    def cancel_by_ids(
        self, occurrence_ids: Sequence[UUID], now: datetime
    ) -> list[PlanOccurrenceRecord]: ...

    def has_unsettled(self, schedule_id: UUID) -> bool: ...
    def has_unsettled_for_task(self, task_id: UUID) -> bool: ...

    def list_by_schedule(
        self,
        schedule_id: UUID,
        *,
        historical: bool,
        after_ordinal: int | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[OccurrenceSummary]: ...


class ExecutionRepository(Protocol):
    def maintenance_impact(self, terminal_id: UUID) -> list[dict[str, str]]: ...

    def list_eligible_resources(
        self,
        *,
        requested_terminal_ids: set[UUID],
        requested_target_device_ids: set[UUID],
        include_unrestricted: bool,
        limit: int,
    ) -> list[EligibleExecutionResource]: ...

    def add(self, execution: ExecutionRecord) -> None: ...

    def get_for_update(self, execution_id: UUID) -> ExecutionRecord | None: ...

    def get(self, execution_id: UUID) -> ExecutionRecord | None: ...

    def get_by_task_for_update(self, task_id: UUID) -> ExecutionRecord | None: ...

    def get_by_task(self, task_id: UUID) -> ExecutionRecord | None: ...

    def request_cancel(
        self,
        execution_id: UUID,
        expected_version: int,
        now: datetime,
        *,
        reason: str,
        deadline_at: datetime | None,
    ) -> ExecutionRecord | None: ...

    def acknowledge_cancel_delivery(
        self,
        execution_id: UUID,
        expected_version: int,
        *,
        deadline_at: datetime | None,
        delayed: bool,
        note: str | None,
    ) -> ExecutionRecord | None: ...

    def touch_waiting(
        self, execution_id: UUID, expected_version: int
    ) -> ExecutionRecord | None: ...

    def set_status(
        self,
        execution_id: UUID,
        expected_version: int,
        status: str,
        *,
        result: str | None,
        now: datetime,
        timeout_at: datetime | None,
    ) -> ExecutionRecord | None: ...

    def cancel_not_running(
        self, schedule_id: UUID, now: datetime
    ) -> tuple[list[ExecutionRecord], list[ExecutionAttemptRecord]]: ...

    def has_running(self, schedule_id: UUID) -> bool: ...

    def list_runtime_timeouts_for_update(
        self, *, now: datetime, limit: int
    ) -> list[RuntimeTimeoutCandidate]: ...

    def list_cleanup_deadlines_for_update(
        self, *, now: datetime, limit: int
    ) -> list[CleanupDeadlineCandidate]: ...


class AttemptRepository(Protocol):
    def add(self, attempt: ExecutionAttemptRecord) -> None: ...

    def get_for_update(self, attempt_id: UUID) -> ExecutionAttemptRecord | None: ...

    def list_by_execution(
        self, execution_id: UUID, *, limit: int
    ) -> list[ExecutionAttemptRecord]: ...

    def get_active_for_execution(self, execution_id: UUID) -> ExecutionAttemptRecord | None: ...

    def is_next_for_resources(
        self,
        attempt_id: UUID,
        *,
        terminal_id: UUID,
        target_device_id: UUID | None,
        now: datetime,
    ) -> bool: ...

    def set_status(
        self,
        attempt_id: UUID,
        expected_version: int,
        status: str,
        *,
        result: str | None,
        now: datetime,
        error_code: str | None,
        retryable: bool | None,
        failure_phase: str | None,
        lease_id: UUID | None = None,
    ) -> ExecutionAttemptRecord | None: ...


class SnapshotRepository(Protocol):
    def ready_blob_ids(self, blob_ids: set[UUID]) -> set[UUID]: ...

    def add(
        self,
        snapshot: ExecutionSnapshotRecord,
        blobs: Sequence[SnapshotBlobReference],
    ) -> None: ...

    def get_by_execution(self, execution_id: UUID) -> ExecutionSnapshotRecord | None: ...

    def list_blobs(self, snapshot_id: UUID) -> tuple[SnapshotBlobReference, ...]: ...

    def list_package_resources(self, snapshot_id: UUID) -> tuple[PackageResource, ...]: ...


class TaskRetryOriginRepository(Protocol):
    def add(self, origin: TaskRetryOriginRecord) -> None: ...

    def get_by_task(self, task_id: UUID) -> TaskRetryOriginRecord | None: ...


class TransitionRepository(Protocol):
    def add_many(self, transitions: Sequence[StateTransitionRecord]) -> None: ...

    def list_for_execution(
        self,
        execution_id: UUID,
        attempt_ids: Sequence[UUID],
        *,
        limit: int,
    ) -> list[StateTransitionRecord]: ...


class SchedulingUnitOfWork(Protocol):
    tasks: TaskRepository
    schedules: ScheduleRepository
    schedule_revisions: ScheduleRevisionRepository
    occurrences: OccurrenceRepository
    executions: ExecutionRepository
    attempts: AttemptRepository
    snapshots: SnapshotRepository
    retry_origins: TaskRetryOriginRepository
    transitions: TransitionRepository
    packages: TaskPackageRepository
    commands: TerminalCommandRepository
    terminal_reports: TerminalReportRepository
    command_acknowledgements: CommandAcknowledgementRepository
    offline_permits: OfflineStartPermitRepository
    leases: ExecutionLeaseRepository
    outbox: OutboxRepository
    audit: AuditRepository

    def __enter__(self) -> SchedulingUnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def flush(self) -> None: ...
