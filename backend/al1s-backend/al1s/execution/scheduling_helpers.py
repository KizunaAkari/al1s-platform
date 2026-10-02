from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime
from uuid import UUID, uuid4

from al1s.execution.errors import ConflictError, InvalidRequestError
from al1s.execution.schedule_calculation import timed_instants
from al1s.execution.scheduling_types import (
    ActiveScheduleManagementSummary,
    ActiveScheduleSummary,
    CapabilityRequirements,
    CreateTaskCommand,
    EligibleExecutionResource,
    ExecutionStatus,
    OccurrenceStatus,
    PlanOccurrenceRecord,
    ScheduleRevisionRecord,
    ScheduleRevisionTimeRecord,
    ScheduleStatus,
    StateTransitionRecord,
    TaskHistoryManagementSummary,
    TaskHistorySummary,
    TaskLifecycleStatus,
    TaskRequestRecord,
    TaskScheduleRecord,
    TaskType,
)
from al1s.execution.types import ActionAvailability

MAX_OCCURRENCES = 10_000


def _validate_cursor(cursor_time: datetime | None, cursor_id: UUID | None, limit: int) -> None:
    if not 1 <= limit <= 100:
        raise InvalidRequestError("invalid_page_limit", "Page limit must be between 1 and 100")
    if (cursor_time is None) != (cursor_id is None):
        raise InvalidRequestError(
            "invalid_page_cursor", "Cursor timestamp and identifier must be supplied together"
        )
    if cursor_time is not None and cursor_time.utcoffset() is None:
        raise InvalidRequestError("invalid_page_cursor", "Cursor timestamp must include a timezone")


def _validate_ordinal_cursor(after_ordinal: int | None, after_id: UUID | None, limit: int) -> None:
    if not 1 <= limit <= 100:
        raise InvalidRequestError("invalid_page_limit", "Page limit must be between 1 and 100")
    if (after_ordinal is None) != (after_id is None):
        raise InvalidRequestError(
            "invalid_page_cursor", "Cursor ordinal and identifier must be supplied together"
        )
    if after_ordinal is not None and after_ordinal < 1:
        raise InvalidRequestError("invalid_page_cursor", "Cursor ordinal must be positive")


def _allowed() -> ActionAvailability:
    return ActionAvailability(allowed=True)


def _refused(code: str, message: str) -> ActionAvailability:
    return ActionAvailability(allowed=False, refusal_code=code, refusal_message=message)


def _task_history_management(item: TaskHistorySummary) -> TaskHistoryManagementSummary:
    if item.task_type not in {TaskType.SINGLE, TaskType.BATCH}:
        cancel = _refused(
            "task_cancel_requires_single",
            "Loop and timed tasks must be terminated through their schedule",
        )
    elif item.lifecycle_status is not TaskLifecycleStatus.ACTIVE or (
        item.task_type is TaskType.SINGLE
        and item.latest_execution_status
        in {
            ExecutionStatus.ENDED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMED_OUT,
        }
    ):
        cancel = _refused("task_not_cancellable", "Task is already in a terminal state")
    else:
        cancel = _allowed()

    delete = (
        _refused(
            "active_task_delete_forbidden",
            "Active tasks must be cancelled or terminated before deleting history",
        )
        if item.lifecycle_status is TaskLifecycleStatus.ACTIVE
        else _allowed()
    )
    return TaskHistoryManagementSummary(task=item, cancel=cancel, delete=delete)


def _active_schedule_management(
    item: ActiveScheduleSummary,
) -> ActiveScheduleManagementSummary:
    if item.status is ScheduleStatus.ACTIVE:
        pause = _allowed()
        resume = _refused("schedule_not_paused", "Only paused schedules can be resumed")
        terminate = _allowed()
    elif item.status is ScheduleStatus.PAUSED:
        pause = _refused("schedule_already_paused", "Schedule is already paused")
        resume = _allowed()
        terminate = _allowed()
    else:
        pause = _refused("schedule_terminating", "Schedule termination is already in progress")
        resume = _refused("schedule_terminating", "Schedule termination is already in progress")
        terminate = _refused("schedule_terminating", "Schedule termination is already in progress")

    revise = (
        _allowed()
        if item.schedule_type is TaskType.TIMED
        and item.status in {ScheduleStatus.ACTIVE, ScheduleStatus.PAUSED}
        else _refused(
            "schedule_revision_requires_active_timed",
            "Only active or paused timed schedules can be revised",
        )
    )
    return ActiveScheduleManagementSummary(
        schedule=item,
        pause=pause,
        resume=resume,
        terminate=terminate,
        revise=revise,
    )


def _build_plan(
    command: CreateTaskCommand,
    task_id: UUID,
    now: datetime,
) -> tuple[
    TaskScheduleRecord | None,
    ScheduleRevisionRecord | None,
    list[ScheduleRevisionTimeRecord],
    list[PlanOccurrenceRecord],
]:
    if command.task_type is TaskType.SINGLE:
        return None, None, [], [_occurrence(task_id, None, None, 1, now, now)]
    schedule_id = uuid4()
    if command.task_type is TaskType.LOOP:
        assert command.loop is not None
        if not 1 <= command.loop.repeat_count <= MAX_OCCURRENCES:
            raise InvalidRequestError(
                "invalid_repeat_count",
                f"Loop repeat count must be between 1 and {MAX_OCCURRENCES}",
            )
        count = command.loop.repeat_count
        schedule = TaskScheduleRecord(
            schedule_id=schedule_id,
            task_request_id=task_id,
            schedule_type=TaskType.LOOP,
            status=ScheduleStatus.ACTIVE,
            total_occurrences=count,
            repeat_count=count,
            timezone=None,
            start_date=None,
            end_date=None,
            current_revision=None,
            paused_at=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
            row_version=1,
        )
        return (
            schedule,
            None,
            [],
            [
                _occurrence(task_id, schedule_id, None, ordinal, None, now)
                for ordinal in range(1, count + 1)
            ],
        )
    assert command.timed is not None
    utc_instants = timed_instants(command.timed, maximum=MAX_OCCURRENCES)
    if not utc_instants or len(utc_instants) > MAX_OCCURRENCES:
        raise InvalidRequestError(
            "invalid_timed_occurrence_count",
            f"Timed task must create between 1 and {MAX_OCCURRENCES} occurrences",
        )
    daily_times = tuple(sorted(set(command.timed.daily_times)))
    schedule_revision_id = uuid4()
    schedule = TaskScheduleRecord(
        schedule_id=schedule_id,
        task_request_id=task_id,
        schedule_type=TaskType.TIMED,
        status=ScheduleStatus.ACTIVE,
        total_occurrences=len(utc_instants),
        repeat_count=None,
        timezone=command.timed.timezone,
        start_date=command.timed.start_date,
        end_date=command.timed.end_date,
        current_revision=1,
        paused_at=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
        row_version=1,
    )
    revision = ScheduleRevisionRecord(
        schedule_revision_id=schedule_revision_id,
        schedule_id=schedule_id,
        revision=1,
        timezone=command.timed.timezone,
        start_date=command.timed.start_date,
        end_date=command.timed.end_date,
        created_at=now,
    )
    revision_times = [
        ScheduleRevisionTimeRecord(uuid4(), schedule_revision_id, ordinal, local_time)
        for ordinal, local_time in enumerate(daily_times, start=1)
    ]
    occurrences = [
        _occurrence(task_id, schedule_id, schedule_revision_id, ordinal, instant, now)
        for ordinal, instant in enumerate(utc_instants, start=1)
    ]
    return schedule, revision, revision_times, occurrences


def _occurrence(
    task_id: UUID,
    schedule_id: UUID | None,
    schedule_revision_id: UUID | None,
    ordinal: int,
    scheduled_for: datetime | None,
    now: datetime,
) -> PlanOccurrenceRecord:
    return PlanOccurrenceRecord(
        occurrence_id=uuid4(),
        task_request_id=task_id,
        schedule_id=schedule_id,
        schedule_revision_id=schedule_revision_id,
        ordinal=ordinal,
        scheduled_for=scheduled_for,
        status=OccurrenceStatus.PLANNED,
        materialization_owner=None,
        materialization_expires_at=None,
        created_at=now,
        settled_at=None,
        row_version=1,
    )


def _select_resource(
    task: TaskRequestRecord,
    requirements: CapabilityRequirements,
    resources: list[EligibleExecutionResource],
) -> tuple[EligibleExecutionResource, UUID | None]:
    for resource in resources:
        if task.requested_terminal_id not in (None, resource.terminal_id):
            continue
        if not _matches_capability(requirements, resource):
            continue
        if task.requested_target_device_id is not None:
            if task.requested_target_device_id not in resource.managed_target_device_ids:
                continue
            return resource, task.requested_target_device_id
        if requirements.requires_target_device:
            if not resource.managed_target_device_ids:
                continue
            return resource, resource.managed_target_device_ids[0]
        return resource, None
    raise ConflictError(
        "eligible_terminal_unavailable",
        "No accepting terminal satisfies the execution requirements",
    )


def _matches_capability(
    requirements: CapabilityRequirements,
    resource: EligibleExecutionResource,
) -> bool:
    capability = resource.capability
    return (
        capability.protocol_version >= requirements.min_protocol_version
        and capability.memory_bytes >= requirements.min_memory_bytes
        and capability.storage_available_bytes >= requirements.min_storage_bytes
        and (
            not requirements.architectures or capability.architecture in requirements.architectures
        )
        and (
            requirements.accelerator_type is None
            or capability.accelerator_type == requirements.accelerator_type
        )
        and set(requirements.provider_keys).issubset(capability.provider_keys)
    )


def _request_hash(command: CreateTaskCommand) -> str:
    payload = asdict(command)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"Unsupported request value: {type(value).__name__}")


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
        actor_type="system" if reason_code.startswith("execution_") else "operator",
        actor_id=None,
        reason_code=reason_code,
        correlation_id=correlation_id,
        occurred_at=occurred_at,
    )
