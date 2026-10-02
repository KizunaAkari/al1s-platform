from __future__ import annotations

from uuid import UUID

from al1s.execution.errors import ConflictError, NotFoundError
from al1s.execution.scheduling_base import SchedulingContext
from al1s.execution.scheduling_helpers import (
    _transition,
)
from al1s.execution.scheduling_state import (
    require_schedule_transition,
)
from al1s.execution.scheduling_types import (
    AttemptStatus,
    ExecutionStatus,
    OccurrenceStatus,
    ScheduleStatus,
    TaskLifecycleStatus,
    TaskScheduleRecord,
    TaskType,
)


class ScheduleCommands(SchedulingContext):
    def pause_schedule(
        self, schedule_id: UUID, *, expected_version: int, correlation_id: UUID
    ) -> TaskScheduleRecord:
        now = self._now()
        with self._uow_factory() as uow:
            schedule = self._require_schedule(uow, schedule_id)
            require_schedule_transition(schedule.status, ScheduleStatus.PAUSED)
            if schedule.row_version != expected_version:
                self._stale_schedule()
            skipped = (
                uow.occurrences.skip_overdue_timed(schedule_id, now)
                if schedule.schedule_type is TaskType.TIMED
                else []
            )
            updated = uow.schedules.set_status(
                schedule_id,
                expected_version,
                ScheduleStatus.PAUSED.value,
                now,
                paused_at=now,
                completed_at=None,
            )
            if updated is None:
                self._stale_schedule()
            assert updated is not None
            uow.transitions.add_many(
                [
                    _transition(
                        "task_schedule",
                        schedule_id,
                        schedule.status.value,
                        updated.status.value,
                        "operator_paused",
                        correlation_id,
                        now,
                    ),
                    *[
                        _transition(
                            "plan_occurrence",
                            item.occurrence_id,
                            OccurrenceStatus.PLANNED.value,
                            item.status.value,
                            "schedule_paused_due_occurrence_skipped",
                            correlation_id,
                            now,
                        )
                        for item in skipped
                    ],
                ]
            )
            self._record_schedule_change(uow, updated, "paused", correlation_id, now, len(skipped))
            uow.commit()
        return updated

    def resume_schedule(
        self, schedule_id: UUID, *, expected_version: int, correlation_id: UUID
    ) -> TaskScheduleRecord:
        now = self._now()
        with self._uow_factory() as uow:
            schedule = self._require_schedule(uow, schedule_id)
            require_schedule_transition(schedule.status, ScheduleStatus.ACTIVE)
            if schedule.row_version != expected_version:
                self._stale_schedule()
            skipped = (
                uow.occurrences.skip_overdue_timed(schedule_id, now)
                if schedule.schedule_type is TaskType.TIMED
                else []
            )
            target_status = (
                ScheduleStatus.ACTIVE
                if uow.occurrences.has_unsettled(schedule_id)
                else ScheduleStatus.COMPLETED
            )
            require_schedule_transition(schedule.status, target_status)
            updated = uow.schedules.set_status(
                schedule_id,
                expected_version,
                target_status.value,
                now,
                paused_at=None,
                completed_at=now if target_status is ScheduleStatus.COMPLETED else None,
            )
            if updated is None:
                self._stale_schedule()
            assert updated is not None
            schedule_reason = (
                "operator_resumed"
                if target_status is ScheduleStatus.ACTIVE
                else "schedule_elapsed_while_paused"
            )
            transitions = [
                _transition(
                    "task_schedule",
                    schedule_id,
                    schedule.status.value,
                    updated.status.value,
                    schedule_reason,
                    correlation_id,
                    now,
                ),
                *[
                    _transition(
                        "plan_occurrence",
                        item.occurrence_id,
                        OccurrenceStatus.PLANNED.value,
                        item.status.value,
                        "schedule_paused_occurrence_not_replayed",
                        correlation_id,
                        now,
                    )
                    for item in skipped
                ],
            ]
            if target_status is ScheduleStatus.COMPLETED:
                task = uow.tasks.get(schedule.task_request_id, for_update=True)
                if task is None:
                    raise NotFoundError("task")
                updated_task = uow.tasks.set_lifecycle(
                    task.task_id,
                    task.row_version,
                    TaskLifecycleStatus.COMPLETED.value,
                    now,
                )
                if updated_task is None:
                    raise ConflictError(
                        "task_completion_conflict", "Task state changed concurrently"
                    )
                transitions.append(
                    _transition(
                        "task_request",
                        task.task_id,
                        task.lifecycle_status.value,
                        updated_task.lifecycle_status.value,
                        schedule_reason,
                        correlation_id,
                        now,
                    )
                )
            uow.transitions.add_many(transitions)
            self._record_schedule_change(
                uow,
                updated,
                "resumed" if target_status is ScheduleStatus.ACTIVE else "completed",
                correlation_id,
                now,
                len(skipped),
            )
            uow.commit()
        return updated

    def terminate_schedule(
        self, schedule_id: UUID, *, expected_version: int, correlation_id: UUID
    ) -> TaskScheduleRecord:
        now = self._now()
        with self._uow_factory() as uow:
            schedule = self._require_schedule(uow, schedule_id)
            require_schedule_transition(schedule.status, ScheduleStatus.TERMINATING)
            if schedule.row_version != expected_version:
                self._stale_schedule()
            cancelled_occurrences = uow.occurrences.cancel_planned(schedule_id, now)
            cancelled_executions, cancelled_attempts = uow.executions.cancel_not_running(
                schedule_id, now
            )
            has_running = uow.executions.has_running(schedule_id)
            target_status = ScheduleStatus.TERMINATING if has_running else ScheduleStatus.TERMINATED
            updated = uow.schedules.set_status(
                schedule_id,
                expected_version,
                target_status.value,
                now,
                paused_at=None,
                completed_at=now if target_status is ScheduleStatus.TERMINATED else None,
            )
            if updated is None:
                self._stale_schedule()
            assert updated is not None
            transitions = [
                _transition(
                    "task_schedule",
                    schedule_id,
                    schedule.status.value,
                    updated.status.value,
                    "operator_terminated",
                    correlation_id,
                    now,
                )
            ]
            transitions.extend(
                _transition(
                    "plan_occurrence",
                    item.occurrence_id,
                    OccurrenceStatus.PLANNED.value,
                    item.status.value,
                    "schedule_terminated",
                    correlation_id,
                    now,
                )
                for item in cancelled_occurrences
            )
            transitions.extend(
                _transition(
                    "execution",
                    item.execution_id,
                    ExecutionStatus.QUEUED.value,
                    item.status.value,
                    "schedule_terminated",
                    correlation_id,
                    now,
                )
                for item in cancelled_executions
            )
            transitions.extend(
                _transition(
                    "execution_attempt",
                    item.attempt_id,
                    AttemptStatus.QUEUED.value,
                    item.status.value,
                    "schedule_terminated",
                    correlation_id,
                    now,
                )
                for item in cancelled_attempts
            )
            uow.transitions.add_many(transitions)
            if target_status is ScheduleStatus.TERMINATED:
                task = uow.tasks.get(schedule.task_request_id, for_update=True)
                if task is None:
                    raise NotFoundError("task")
                updated_task = uow.tasks.set_lifecycle(
                    task.task_id,
                    task.row_version,
                    TaskLifecycleStatus.TERMINATED.value,
                    now,
                )
                if updated_task is None:
                    raise ConflictError("task_state_conflict", "Task state changed concurrently")
                uow.transitions.add_many(
                    [
                        _transition(
                            "task_request",
                            task.task_id,
                            task.lifecycle_status.value,
                            updated_task.lifecycle_status.value,
                            "schedule_terminated",
                            correlation_id,
                            now,
                        )
                    ]
                )
            self._record_schedule_change(
                uow,
                updated,
                "terminated" if not has_running else "terminating",
                correlation_id,
                now,
                len(cancelled_occurrences),
            )
            uow.commit()
        return updated
