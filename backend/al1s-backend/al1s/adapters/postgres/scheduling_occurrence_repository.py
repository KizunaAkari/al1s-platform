from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from al1s.adapters.postgres.scheduling_models import (
    ExecutionRow,
    PlanOccurrenceRow,
    TaskRequestRow,
    TaskScheduleRevisionRow,
    TaskScheduleRow,
)
from al1s.adapters.postgres.scheduling_repository_records import (
    MAX_WORKER_BATCH,
    _occurrence_record,
    _task_record,
    _validate_ordinal_page,
)
from al1s.execution.scheduling_types import (
    ExecutionResult,
    ExecutionStatus,
    MaterializationCandidate,
    OccurrenceStatus,
    OccurrenceSummary,
    PlanOccurrenceRecord,
    ScheduleStatus,
    TaskLifecycleStatus,
    TaskType,
)


class PostgresOccurrenceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_many(self, occurrences: Sequence[PlanOccurrenceRecord]) -> None:
        self._session.add_all(
            [
                PlanOccurrenceRow(
                    id=item.occurrence_id,
                    task_request_id=item.task_request_id,
                    schedule_id=item.schedule_id,
                    schedule_revision_id=item.schedule_revision_id,
                    ordinal=item.ordinal,
                    scheduled_for=item.scheduled_for,
                    status=item.status.value,
                    materialization_owner=item.materialization_owner,
                    materialization_expires_at=item.materialization_expires_at,
                    created_at=item.created_at,
                    settled_at=item.settled_at,
                    row_version=item.row_version,
                )
                for item in occurrences
            ]
        )

    def claim_due(
        self,
        *,
        worker_id: UUID,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
    ) -> list[MaterializationCandidate]:
        if not 1 <= limit <= MAX_WORKER_BATCH:
            raise ValueError(f"limit must be between 1 and {MAX_WORKER_BATCH}")
        previous_occurrence = aliased(PlanOccurrenceRow)
        previous_execution = aliased(ExecutionRow)
        due = or_(
            and_(
                PlanOccurrenceRow.schedule_id.is_(None),
                PlanOccurrenceRow.scheduled_for <= now,
                or_(
                    TaskRequestRow.task_type != TaskType.BATCH.value,
                    PlanOccurrenceRow.ordinal == 1,
                    previous_execution.status.in_(("ended", "cancelled", "timed_out")),
                ),
            ),
            and_(
                TaskScheduleRow.schedule_type == TaskType.TIMED.value,
                TaskScheduleRow.status == ScheduleStatus.ACTIVE.value,
                PlanOccurrenceRow.scheduled_for <= now,
            ),
            and_(
                TaskScheduleRow.schedule_type == TaskType.LOOP.value,
                TaskScheduleRow.status == ScheduleStatus.ACTIVE.value,
                or_(
                    PlanOccurrenceRow.ordinal == 1,
                    previous_execution.status.in_(
                        (
                            ExecutionStatus.ENDED.value,
                            ExecutionStatus.CANCELLED.value,
                            ExecutionStatus.TIMED_OUT.value,
                        )
                    ),
                ),
            ),
        )
        claimable_ids = (
            select(PlanOccurrenceRow.id)
            .join(TaskRequestRow, TaskRequestRow.id == PlanOccurrenceRow.task_request_id)
            .outerjoin(TaskScheduleRow, TaskScheduleRow.id == PlanOccurrenceRow.schedule_id)
            .outerjoin(
                previous_occurrence,
                and_(
                    previous_occurrence.task_request_id == PlanOccurrenceRow.task_request_id,
                    previous_occurrence.ordinal == PlanOccurrenceRow.ordinal - 1,
                ),
            )
            .outerjoin(
                previous_execution,
                previous_execution.occurrence_id == previous_occurrence.id,
            )
            .where(
                TaskRequestRow.lifecycle_status == TaskLifecycleStatus.ACTIVE.value,
                PlanOccurrenceRow.status == OccurrenceStatus.PLANNED.value,
                or_(
                    PlanOccurrenceRow.materialization_owner.is_(None),
                    PlanOccurrenceRow.materialization_expires_at <= now,
                ),
                due,
            )
            .order_by(
                func.coalesce(PlanOccurrenceRow.scheduled_for, PlanOccurrenceRow.created_at),
                TaskRequestRow.created_at,
                PlanOccurrenceRow.ordinal,
                PlanOccurrenceRow.id,
            )
            .limit(limit)
            .with_for_update(of=PlanOccurrenceRow, skip_locked=True)
            .cte("claimable_occurrences")
        )
        claimed_rows = list(
            self._session.execute(
                update(PlanOccurrenceRow)
                .where(PlanOccurrenceRow.id.in_(select(claimable_ids.c.id)))
                .values(
                    materialization_owner=worker_id,
                    materialization_expires_at=now + lease_duration,
                    row_version=PlanOccurrenceRow.row_version + 1,
                )
                .returning(PlanOccurrenceRow)
            )
            .scalars()
            .all()
        )
        if not claimed_rows:
            return []
        task_rows = self._session.scalars(
            select(TaskRequestRow).where(
                TaskRequestRow.id.in_({row.task_request_id for row in claimed_rows})
            )
        ).all()
        tasks = {row.id: _task_record(row) for row in task_rows}
        claimed_rows.sort(
            key=lambda row: (row.scheduled_for or row.created_at, row.ordinal, row.id)
        )
        return [
            MaterializationCandidate(
                occurrence=_occurrence_record(row),
                task=tasks[row.task_request_id],
            )
            for row in claimed_rows
        ]

    def mark_materialized(
        self,
        occurrence_id: UUID,
        expected_owner: UUID,
        settled_at: datetime,
    ) -> PlanOccurrenceRecord | None:
        row = self._session.execute(
            update(PlanOccurrenceRow)
            .where(
                PlanOccurrenceRow.id == occurrence_id,
                PlanOccurrenceRow.status == OccurrenceStatus.PLANNED.value,
                PlanOccurrenceRow.materialization_owner == expected_owner,
                PlanOccurrenceRow.materialization_expires_at > settled_at,
            )
            .values(
                status=OccurrenceStatus.MATERIALIZED.value,
                settled_at=settled_at,
                materialization_owner=None,
                materialization_expires_at=None,
                row_version=PlanOccurrenceRow.row_version + 1,
            )
            .returning(PlanOccurrenceRow)
        ).scalar_one_or_none()
        return _occurrence_record(row) if row else None

    def skip_overdue_timed(self, schedule_id: UUID, now: datetime) -> list[PlanOccurrenceRecord]:
        rows = (
            self._session.execute(
                update(PlanOccurrenceRow)
                .where(
                    PlanOccurrenceRow.schedule_id == schedule_id,
                    PlanOccurrenceRow.status == OccurrenceStatus.PLANNED.value,
                    PlanOccurrenceRow.scheduled_for <= now,
                )
                .values(
                    status=OccurrenceStatus.SKIPPED.value,
                    settled_at=now,
                    materialization_owner=None,
                    materialization_expires_at=None,
                    row_version=PlanOccurrenceRow.row_version + 1,
                )
                .returning(PlanOccurrenceRow)
            )
            .scalars()
            .all()
        )
        return [_occurrence_record(row) for row in rows]

    def cancel_planned(self, schedule_id: UUID, now: datetime) -> list[PlanOccurrenceRecord]:
        rows = (
            self._session.execute(
                update(PlanOccurrenceRow)
                .where(
                    PlanOccurrenceRow.schedule_id == schedule_id,
                    PlanOccurrenceRow.status == OccurrenceStatus.PLANNED.value,
                )
                .values(
                    status=OccurrenceStatus.CANCELLED.value,
                    settled_at=now,
                    materialization_owner=None,
                    materialization_expires_at=None,
                    row_version=PlanOccurrenceRow.row_version + 1,
                )
                .returning(PlanOccurrenceRow)
            )
            .scalars()
            .all()
        )
        return [_occurrence_record(row) for row in rows]

    def cancel_planned_for_task(self, task_id: UUID, now: datetime) -> list[PlanOccurrenceRecord]:
        rows = (
            self._session.execute(
                update(PlanOccurrenceRow)
                .where(
                    PlanOccurrenceRow.task_request_id == task_id,
                    PlanOccurrenceRow.status == OccurrenceStatus.PLANNED.value,
                )
                .values(
                    status=OccurrenceStatus.CANCELLED.value,
                    settled_at=now,
                    materialization_owner=None,
                    materialization_expires_at=None,
                    row_version=PlanOccurrenceRow.row_version + 1,
                )
                .returning(PlanOccurrenceRow)
            )
            .scalars()
            .all()
        )
        return [_occurrence_record(row) for row in rows]

    def list_for_schedule_update(self, schedule_id: UUID) -> list[PlanOccurrenceRecord]:
        rows = self._session.scalars(
            select(PlanOccurrenceRow)
            .where(PlanOccurrenceRow.schedule_id == schedule_id)
            .order_by(PlanOccurrenceRow.ordinal, PlanOccurrenceRow.id)
            .with_for_update()
        ).all()
        return [_occurrence_record(row) for row in rows]

    def cancel_by_ids(
        self, occurrence_ids: Sequence[UUID], now: datetime
    ) -> list[PlanOccurrenceRecord]:
        if not occurrence_ids:
            return []
        rows = self._session.execute(
            update(PlanOccurrenceRow)
            .where(
                PlanOccurrenceRow.id.in_(occurrence_ids),
                PlanOccurrenceRow.status == OccurrenceStatus.PLANNED.value,
            )
            .values(
                status=OccurrenceStatus.CANCELLED.value,
                materialization_owner=None,
                materialization_expires_at=None,
                settled_at=now,
                row_version=PlanOccurrenceRow.row_version + 1,
            )
            .returning(PlanOccurrenceRow)
        ).scalars()
        return [_occurrence_record(row) for row in rows]

    def has_unsettled(self, schedule_id: UUID) -> bool:
        return (
            self._session.scalar(
                select(PlanOccurrenceRow.id)
                .outerjoin(ExecutionRow, ExecutionRow.occurrence_id == PlanOccurrenceRow.id)
                .where(
                    PlanOccurrenceRow.schedule_id == schedule_id,
                    or_(
                        PlanOccurrenceRow.status == OccurrenceStatus.PLANNED.value,
                        ExecutionRow.status.in_(
                            (
                                ExecutionStatus.WAITING.value,
                                ExecutionStatus.QUEUED.value,
                                ExecutionStatus.RUNNING.value,
                            )
                        ),
                    ),
                )
                .limit(1)
            )
            is not None
        )

    def has_unsettled_for_task(self, task_id: UUID) -> bool:
        return (
            self._session.scalar(
                select(PlanOccurrenceRow.id)
                .outerjoin(ExecutionRow, ExecutionRow.occurrence_id == PlanOccurrenceRow.id)
                .where(
                    PlanOccurrenceRow.task_request_id == task_id,
                    or_(
                        PlanOccurrenceRow.status == "planned",
                        ExecutionRow.status.in_(("waiting", "queued", "running")),
                    ),
                )
                .limit(1)
            )
            is not None
        )

    def list_by_schedule(
        self,
        schedule_id: UUID,
        *,
        historical: bool,
        after_ordinal: int | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[OccurrenceSummary]:
        _validate_ordinal_page(after_ordinal, after_id, limit)
        if historical:
            statement = (
                select(PlanOccurrenceRow, ExecutionRow, TaskScheduleRevisionRow.revision)
                .outerjoin(ExecutionRow, ExecutionRow.occurrence_id == PlanOccurrenceRow.id)
                .outerjoin(
                    TaskScheduleRevisionRow,
                    TaskScheduleRevisionRow.id == PlanOccurrenceRow.schedule_revision_id,
                )
                .where(
                    PlanOccurrenceRow.schedule_id == schedule_id,
                    PlanOccurrenceRow.status == OccurrenceStatus.CANCELLED.value,
                )
                .order_by(PlanOccurrenceRow.ordinal, PlanOccurrenceRow.id)
                .limit(limit)
            )
            if after_ordinal is not None and after_id is not None:
                statement = statement.where(
                    or_(
                        PlanOccurrenceRow.ordinal > after_ordinal,
                        and_(
                            PlanOccurrenceRow.ordinal == after_ordinal,
                            PlanOccurrenceRow.id > after_id,
                        ),
                    )
                )
            return [
                OccurrenceSummary(
                    occurrence=_occurrence_record(occurrence),
                    display_ordinal=None,
                    schedule_revision=(int(revision) if revision is not None else None),
                    execution_id=execution.id if execution else None,
                    execution_status=(ExecutionStatus(execution.status) if execution else None),
                    execution_result=(
                        ExecutionResult(execution.result)
                        if execution and execution.result
                        else None
                    ),
                )
                for occurrence, execution, revision in self._session.execute(statement).all()
            ]

        ranked = (
            select(
                PlanOccurrenceRow.id.label("occurrence_id"),
                func.row_number()
                .over(
                    order_by=(
                        PlanOccurrenceRow.scheduled_for.asc().nulls_last(),
                        PlanOccurrenceRow.ordinal,
                        PlanOccurrenceRow.id,
                    )
                )
                .label("display_ordinal"),
            )
            .where(
                PlanOccurrenceRow.schedule_id == schedule_id,
                PlanOccurrenceRow.status != OccurrenceStatus.CANCELLED.value,
            )
            .subquery()
        )
        statement = (
            select(
                PlanOccurrenceRow,
                ExecutionRow,
                TaskScheduleRevisionRow.revision,
                ranked.c.display_ordinal,
            )
            .join(ranked, ranked.c.occurrence_id == PlanOccurrenceRow.id)
            .outerjoin(ExecutionRow, ExecutionRow.occurrence_id == PlanOccurrenceRow.id)
            .outerjoin(
                TaskScheduleRevisionRow,
                TaskScheduleRevisionRow.id == PlanOccurrenceRow.schedule_revision_id,
            )
            .order_by(ranked.c.display_ordinal, PlanOccurrenceRow.id)
            .limit(limit)
        )
        if after_ordinal is not None and after_id is not None:
            statement = statement.where(
                or_(
                    ranked.c.display_ordinal > after_ordinal,
                    and_(
                        ranked.c.display_ordinal == after_ordinal,
                        PlanOccurrenceRow.id > after_id,
                    ),
                )
            )
        rows = self._session.execute(statement).all()
        return [
            OccurrenceSummary(
                occurrence=_occurrence_record(occurrence),
                display_ordinal=(int(display_ordinal) if display_ordinal is not None else None),
                schedule_revision=(int(revision) if revision is not None else None),
                execution_id=execution.id if execution else None,
                execution_status=ExecutionStatus(execution.status) if execution else None,
                execution_result=(
                    ExecutionResult(execution.result) if execution and execution.result else None
                ),
            )
            for occurrence, execution, revision, display_ordinal in rows
        ]
