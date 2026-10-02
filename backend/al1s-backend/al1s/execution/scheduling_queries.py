from __future__ import annotations

from datetime import datetime
from uuid import UUID

from al1s.execution.errors import ConflictError, NotFoundError
from al1s.execution.scheduling_base import SchedulingContext
from al1s.execution.scheduling_helpers import (
    _active_schedule_management,
    _task_history_management,
    _validate_cursor,
    _validate_ordinal_cursor,
)
from al1s.execution.scheduling_types import (
    ActiveScheduleManagementSummary,
    CreatedTask,
    ExecutionDetails,
    OccurrenceSummary,
    TaskHistoryManagementSummary,
)


class SchedulingQueries(SchedulingContext):
    def get_task(self, task_id: UUID) -> CreatedTask:
        with self._uow_factory() as uow:
            task = uow.tasks.get_created_task(task_id)
        if task is None:
            raise NotFoundError("task")
        return task

    def list_task_history(
        self,
        *,
        before_created_at: datetime | None = None,
        before_id: UUID | None = None,
        limit: int = 50,
    ) -> list[TaskHistoryManagementSummary]:
        _validate_cursor(before_created_at, before_id, limit)
        with self._uow_factory() as uow:
            items = uow.tasks.list_history(
                before_created_at=before_created_at,
                before_id=before_id,
                limit=limit,
            )
        return [_task_history_management(item) for item in items]

    def list_active_schedules(
        self,
        *,
        cursor_updated_at: datetime | None = None,
        cursor_id: UUID | None = None,
        limit: int = 50,
        descending: bool = False,
    ) -> list[ActiveScheduleManagementSummary]:
        _validate_cursor(cursor_updated_at, cursor_id, limit)
        with self._uow_factory() as uow:
            items = uow.schedules.list_active(
                cursor_updated_at=cursor_updated_at,
                cursor_id=cursor_id,
                limit=limit,
                descending=descending,
            )
        return [_active_schedule_management(item) for item in items]

    def list_schedule_occurrences(
        self,
        schedule_id: UUID,
        *,
        historical: bool = False,
        after_ordinal: int | None = None,
        after_id: UUID | None = None,
        limit: int = 50,
    ) -> list[OccurrenceSummary]:
        _validate_ordinal_cursor(after_ordinal, after_id, limit)
        with self._uow_factory() as uow:
            if uow.schedules.get(schedule_id) is None:
                raise NotFoundError("task_schedule")
            return uow.occurrences.list_by_schedule(
                schedule_id,
                historical=historical,
                after_ordinal=after_ordinal,
                after_id=after_id,
                limit=limit,
            )

    def maintenance_impact(self, terminal_id: UUID) -> list[dict[str, str]]:
        with self._uow_factory() as uow:
            return uow.executions.maintenance_impact(terminal_id)

    def get_execution_details(self, execution_id: UUID) -> ExecutionDetails:
        with self._uow_factory() as uow:
            execution = uow.executions.get(execution_id)
            if execution is None:
                raise NotFoundError("execution")
            attempts = uow.attempts.list_by_execution(execution_id, limit=101)
            snapshot = uow.snapshots.get_by_execution(execution_id)
            if snapshot is None:
                raise ConflictError("execution_snapshot_missing", "Execution snapshot is missing")
            transitions = uow.transitions.list_for_execution(
                execution_id,
                [item.attempt_id for item in attempts],
                limit=100,
            )
        return ExecutionDetails(execution, tuple(attempts), snapshot, tuple(transitions))
