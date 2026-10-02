from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from al1s.adapters.postgres.scheduling_models import (
    ExecutionRow,
    PlanOccurrenceRow,
    TaskRequestRow,
    TaskScheduleRevisionRow,
    TaskScheduleRevisionTimeRow,
    TaskScheduleRow,
)
from al1s.adapters.postgres.scheduling_repository_records import (
    _schedule_record,
    _validate_page,
)
from al1s.execution.scheduling_types import (
    ActiveScheduleSummary,
    ExecutionStatus,
    OccurrenceStatus,
    ScheduleStatus,
    TaskScheduleRecord,
    TaskType,
)


class PostgresScheduleRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, schedule: TaskScheduleRecord) -> None:
        self._session.add(
            TaskScheduleRow(
                id=schedule.schedule_id,
                task_request_id=schedule.task_request_id,
                schedule_type=schedule.schedule_type.value,
                status=schedule.status.value,
                total_occurrences=schedule.total_occurrences,
                repeat_count=schedule.repeat_count,
                timezone=schedule.timezone,
                start_date=schedule.start_date,
                end_date=schedule.end_date,
                current_revision=schedule.current_revision,
                paused_at=schedule.paused_at,
                created_at=schedule.created_at,
                updated_at=schedule.updated_at,
                completed_at=schedule.completed_at,
                row_version=schedule.row_version,
            )
        )

    def get_for_update(self, schedule_id: UUID) -> TaskScheduleRecord | None:
        row = self._session.scalar(
            select(TaskScheduleRow).where(TaskScheduleRow.id == schedule_id).with_for_update()
        )
        return _schedule_record(row) if row else None

    def get(self, schedule_id: UUID) -> TaskScheduleRecord | None:
        row = self._session.get(TaskScheduleRow, schedule_id)
        return _schedule_record(row) if row else None

    def get_by_task_for_update(self, task_id: UUID) -> TaskScheduleRecord | None:
        row = self._session.scalar(
            select(TaskScheduleRow)
            .where(TaskScheduleRow.task_request_id == task_id)
            .with_for_update()
        )
        return _schedule_record(row) if row else None

    def set_status(
        self,
        schedule_id: UUID,
        expected_version: int,
        status: str,
        now: datetime,
        *,
        paused_at: datetime | None,
        completed_at: datetime | None,
    ) -> TaskScheduleRecord | None:
        row = self._session.execute(
            update(TaskScheduleRow)
            .where(
                TaskScheduleRow.id == schedule_id,
                TaskScheduleRow.row_version == expected_version,
            )
            .values(
                status=status,
                paused_at=paused_at,
                completed_at=completed_at,
                updated_at=now,
                row_version=TaskScheduleRow.row_version + 1,
            )
            .returning(TaskScheduleRow)
        ).scalar_one_or_none()
        return _schedule_record(row) if row else None

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
    ) -> TaskScheduleRecord | None:
        row = self._session.execute(
            update(TaskScheduleRow)
            .where(
                TaskScheduleRow.id == schedule_id,
                TaskScheduleRow.row_version == expected_version,
            )
            .values(
                total_occurrences=total_occurrences,
                start_date=start_date,
                end_date=end_date,
                current_revision=current_revision,
                updated_at=now,
                row_version=TaskScheduleRow.row_version + 1,
            )
            .returning(TaskScheduleRow)
        ).scalar_one_or_none()
        return _schedule_record(row) if row else None

    def list_active(
        self,
        *,
        cursor_updated_at: datetime | None,
        cursor_id: UUID | None,
        limit: int,
        descending: bool,
    ) -> list[ActiveScheduleSummary]:
        _validate_page(cursor_updated_at, cursor_id, limit)
        current_revision_id = (
            select(TaskScheduleRevisionRow.id)
            .where(
                TaskScheduleRevisionRow.schedule_id == TaskScheduleRow.id,
                TaskScheduleRevisionRow.revision == TaskScheduleRow.current_revision,
            )
            .scalar_subquery()
        )
        daily_times = (
            select(
                func.array_agg(
                    aggregate_order_by(
                        TaskScheduleRevisionTimeRow.local_time,
                        TaskScheduleRevisionTimeRow.ordinal,
                    )
                )
            )
            .where(TaskScheduleRevisionTimeRow.schedule_revision_id == current_revision_id)
            .scalar_subquery()
        )
        statement = (
            select(TaskRequestRow, TaskScheduleRow, daily_times.label("daily_times"))
            .join(TaskScheduleRow, TaskScheduleRow.task_request_id == TaskRequestRow.id)
            .where(
                TaskRequestRow.deleted_at.is_(None),
                TaskScheduleRow.status.in_(
                    (
                        ScheduleStatus.ACTIVE.value,
                        ScheduleStatus.PAUSED.value,
                        ScheduleStatus.TERMINATING.value,
                    )
                ),
            )
            .limit(limit)
        )
        if cursor_updated_at is not None and cursor_id is not None:
            time_comparison = (
                TaskScheduleRow.updated_at < cursor_updated_at
                if descending
                else TaskScheduleRow.updated_at > cursor_updated_at
            )
            id_comparison = (
                TaskScheduleRow.id < cursor_id if descending else TaskScheduleRow.id > cursor_id
            )
            statement = statement.where(
                or_(
                    time_comparison,
                    and_(TaskScheduleRow.updated_at == cursor_updated_at, id_comparison),
                )
            )
        statement = statement.order_by(
            TaskScheduleRow.updated_at.desc() if descending else TaskScheduleRow.updated_at,
            TaskScheduleRow.id.desc() if descending else TaskScheduleRow.id,
        )
        rows = self._session.execute(statement).all()
        if not rows:
            return []
        schedule_ids = [schedule.id for _, schedule, _ in rows]
        active_execution_statuses = (
            ExecutionStatus.WAITING.value,
            ExecutionStatus.QUEUED.value,
            ExecutionStatus.RUNNING.value,
        )
        ranked_occurrences = (
            select(
                PlanOccurrenceRow.id.label("occurrence_id"),
                PlanOccurrenceRow.schedule_id.label("schedule_id"),
                func.row_number()
                .over(
                    partition_by=PlanOccurrenceRow.schedule_id,
                    order_by=(
                        PlanOccurrenceRow.scheduled_for.asc().nulls_last(),
                        PlanOccurrenceRow.ordinal,
                        PlanOccurrenceRow.id,
                    ),
                )
                .label("display_ordinal"),
            )
            .where(
                PlanOccurrenceRow.schedule_id.in_(schedule_ids),
                PlanOccurrenceRow.status != OccurrenceStatus.CANCELLED.value,
            )
            .subquery()
        )
        counts = {
            row.schedule_id: row
            for row in self._session.execute(
                select(
                    PlanOccurrenceRow.schedule_id,
                    func.count(PlanOccurrenceRow.id)
                    .filter(PlanOccurrenceRow.status == OccurrenceStatus.PLANNED.value)
                    .label("planned"),
                    func.count(PlanOccurrenceRow.id)
                    .filter(PlanOccurrenceRow.status == OccurrenceStatus.MATERIALIZED.value)
                    .label("materialized"),
                    func.count(PlanOccurrenceRow.id)
                    .filter(PlanOccurrenceRow.status == OccurrenceStatus.SKIPPED.value)
                    .label("skipped"),
                    func.count(PlanOccurrenceRow.id)
                    .filter(PlanOccurrenceRow.status == OccurrenceStatus.CANCELLED.value)
                    .label("cancelled"),
                    func.count(PlanOccurrenceRow.id)
                    .filter(
                        and_(
                            PlanOccurrenceRow.status == OccurrenceStatus.MATERIALIZED.value,
                            ExecutionRow.status.in_(active_execution_statuses),
                        )
                    )
                    .label("active_materialized"),
                    func.min(ranked_occurrences.c.display_ordinal)
                    .filter(ExecutionRow.status == ExecutionStatus.RUNNING.value)
                    .label("current_ordinal"),
                    func.min(ranked_occurrences.c.display_ordinal)
                    .filter(
                        or_(
                            PlanOccurrenceRow.status == OccurrenceStatus.PLANNED.value,
                            ExecutionRow.status.in_(
                                (
                                    ExecutionStatus.WAITING.value,
                                    ExecutionStatus.QUEUED.value,
                                )
                            ),
                        )
                    )
                    .label("next_ordinal"),
                )
                .outerjoin(ExecutionRow, ExecutionRow.occurrence_id == PlanOccurrenceRow.id)
                .outerjoin(
                    ranked_occurrences,
                    ranked_occurrences.c.occurrence_id == PlanOccurrenceRow.id,
                )
                .where(PlanOccurrenceRow.schedule_id.in_(schedule_ids))
                .group_by(PlanOccurrenceRow.schedule_id)
            ).all()
        }
        return [
            ActiveScheduleSummary(
                task_id=task.id,
                schedule_id=schedule.id,
                name=task.name,
                schedule_type=TaskType(schedule.schedule_type),
                status=ScheduleStatus(schedule.status),
                timezone=schedule.timezone,
                start_date=schedule.start_date,
                end_date=schedule.end_date,
                daily_times=tuple(times or ()),
                current_revision=schedule.current_revision,
                total_occurrences=schedule.total_occurrences,
                settled_occurrences=max(
                    0,
                    schedule.total_occurrences
                    - int(counts[schedule.id].planned)
                    - int(counts[schedule.id].active_materialized),
                ),
                planned_occurrences=int(counts[schedule.id].planned),
                materialized_occurrences=int(counts[schedule.id].materialized),
                skipped_occurrences=int(counts[schedule.id].skipped),
                cancelled_occurrences=int(counts[schedule.id].cancelled),
                current_occurrence_ordinal=(
                    int(counts[schedule.id].current_ordinal)
                    if counts[schedule.id].current_ordinal is not None
                    else None
                ),
                next_occurrence_ordinal=(
                    int(counts[schedule.id].next_ordinal)
                    if counts[schedule.id].next_ordinal is not None
                    else None
                ),
                row_version=schedule.row_version,
                updated_at=schedule.updated_at,
            )
            for task, schedule, times in rows
        ]
