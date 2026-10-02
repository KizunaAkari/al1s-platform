from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.scheduling_models import (
    PlanOccurrenceRow,
    TaskRequestRow,
    TaskScheduleRow,
)
from al1s.execution.scheduling_types import (
    ActiveContentScheduleImpact,
    OccurrenceStatus,
    ScheduleStatus,
    TaskLifecycleStatus,
    TaskType,
)


class PostgresContentScheduleImpactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_active_future_impacts(
        self,
        *,
        source_module: str,
        logical_content_ids: Sequence[str],
    ) -> list[ActiveContentScheduleImpact]:
        if not logical_content_ids:
            return []
        statement = (
            select(
                TaskRequestRow.id,
                TaskScheduleRow.id,
                TaskRequestRow.name,
                TaskRequestRow.task_type,
                TaskScheduleRow.status,
                TaskRequestRow.logical_content_id,
                func.count(PlanOccurrenceRow.id),
                func.min(PlanOccurrenceRow.scheduled_for),
                func.max(PlanOccurrenceRow.scheduled_for),
                TaskRequestRow.row_version,
                TaskScheduleRow.row_version,
            )
            .join(
                TaskScheduleRow,
                TaskScheduleRow.task_request_id == TaskRequestRow.id,
            )
            .join(
                PlanOccurrenceRow,
                PlanOccurrenceRow.schedule_id == TaskScheduleRow.id,
            )
            .where(
                TaskRequestRow.deleted_at.is_(None),
                TaskRequestRow.lifecycle_status == TaskLifecycleStatus.ACTIVE.value,
                TaskRequestRow.source_module == source_module,
                TaskRequestRow.logical_content_id.in_(set(logical_content_ids)),
                TaskScheduleRow.status.in_(
                    (ScheduleStatus.ACTIVE.value, ScheduleStatus.PAUSED.value)
                ),
                PlanOccurrenceRow.status == OccurrenceStatus.PLANNED.value,
            )
            .group_by(
                TaskRequestRow.id,
                TaskScheduleRow.id,
                TaskRequestRow.name,
                TaskRequestRow.task_type,
                TaskScheduleRow.status,
                TaskRequestRow.logical_content_id,
                TaskRequestRow.row_version,
                TaskScheduleRow.row_version,
            )
            .order_by(TaskScheduleRow.id)
        )
        return [
            ActiveContentScheduleImpact(
                task_id=task_id,
                schedule_id=schedule_id,
                task_name=task_name,
                task_type=TaskType(task_type),
                schedule_status=ScheduleStatus(schedule_status),
                logical_content_id=logical_content_id,
                future_occurrence_count=int(future_count),
                first_scheduled_for=first_scheduled_for,
                last_scheduled_for=last_scheduled_for,
                task_row_version=task_row_version,
                schedule_row_version=schedule_row_version,
            )
            for (
                task_id,
                schedule_id,
                task_name,
                task_type,
                schedule_status,
                logical_content_id,
                future_count,
                first_scheduled_for,
                last_scheduled_for,
                task_row_version,
                schedule_row_version,
            ) in self._session.execute(statement).all()
        ]
