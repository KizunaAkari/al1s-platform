from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from al1s.execution.errors import ConflictError, NotFoundError
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_state import require_schedule_transition
from al1s.execution.scheduling_types import (
    ScheduleStatus,
    StateTransitionRecord,
    TaskLifecycleStatus,
    TaskScheduleRecord,
    TaskType,
)


def settle_task_after_execution(
    uow: SchedulingUnitOfWork,
    *,
    task_id: UUID,
    schedule: TaskScheduleRecord | None,
    correlation_id: UUID,
    now: datetime,
    task_target_override: TaskLifecycleStatus | None = None,
) -> None:
    if schedule is not None and uow.occurrences.has_unsettled(schedule.schedule_id):
        return
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError("task")
    if task.task_type is TaskType.BATCH and uow.occurrences.has_unsettled_for_task(task_id):
        return
    task_target = task_target_override or TaskLifecycleStatus.COMPLETED
    transitions: list[StateTransitionRecord] = []
    if schedule is not None:
        schedule_target = (
            ScheduleStatus.TERMINATED
            if schedule.status is ScheduleStatus.TERMINATING
            else ScheduleStatus.COMPLETED
        )
        task_target = (
            TaskLifecycleStatus.TERMINATED
            if schedule_target is ScheduleStatus.TERMINATED
            else TaskLifecycleStatus.COMPLETED
        )
        require_schedule_transition(schedule.status, schedule_target)
        updated_schedule = uow.schedules.set_status(
            schedule.schedule_id,
            schedule.row_version,
            schedule_target.value,
            now,
            paused_at=None,
            completed_at=now,
        )
        if updated_schedule is None:
            raise ConflictError(
                "schedule_completion_conflict", "Task schedule changed concurrently"
            )
        transitions.append(
            _transition(
                "task_schedule",
                schedule.schedule_id,
                schedule.status.value,
                updated_schedule.status.value,
                "schedule_settled",
                correlation_id,
                now,
            )
        )
    updated_task = uow.tasks.set_lifecycle(
        task.task_id,
        task.row_version,
        task_target.value,
        now,
    )
    if updated_task is None:
        raise ConflictError("task_completion_conflict", "Task changed concurrently")
    transitions.append(
        _transition(
            "task_request",
            task.task_id,
            task.lifecycle_status.value,
            updated_task.lifecycle_status.value,
            "task_settled",
            correlation_id,
            now,
        )
    )
    uow.transitions.add_many(transitions)


def _transition(
    aggregate_type: str,
    aggregate_id: UUID,
    from_status: str | None,
    to_status: str,
    reason_code: str,
    correlation_id: UUID,
    now: datetime,
) -> StateTransitionRecord:
    return StateTransitionRecord(
        transition_id=uuid4(),
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        from_status=from_status,
        to_status=to_status,
        actor_type="system",
        actor_id=None,
        reason_code=reason_code,
        correlation_id=correlation_id,
        occurred_at=now,
    )
