from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select, true, update
from sqlalchemy.orm import Session

from al1s.adapters.postgres.lineup_task_history import project_lineup_history
from al1s.adapters.postgres.maa_task_references import lock_active_maa_task_content
from al1s.adapters.postgres.scheduling_models import (
    ExecutionRow,
    PlanOccurrenceRow,
    TaskRequestRow,
    TaskScheduleRow,
)
from al1s.adapters.postgres.scheduling_repository_records import (
    _occurrence_record,
    _schedule_record,
    _task_record,
    _validate_page,
)
from al1s.execution.scheduling_types import (
    CreatedTask,
    ExecutionResult,
    ExecutionStatus,
    ScheduleStatus,
    TaskHistorySummary,
    TaskLifecycleStatus,
    TaskRequestRecord,
    TaskType,
)


class PostgresTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, task: TaskRequestRecord) -> None:
        if (
            task.source_module == "maa"
            and task.lifecycle_status is TaskLifecycleStatus.ACTIVE
            and task.deleted_at is None
        ):
            lock_active_maa_task_content(self._session, task.logical_content_id)
        self._session.add(
            TaskRequestRow(
                id=task.task_id,
                idempotency_key=task.idempotency_key,
                request_hash=task.request_hash,
                name=task.name,
                task_type=task.task_type.value,
                lifecycle_status=task.lifecycle_status.value,
                source_module=task.source_module,
                logical_content_id=task.logical_content_id,
                parameters=dict(task.parameters),
                requested_terminal_id=task.requested_terminal_id,
                requested_target_device_id=task.requested_target_device_id,
                timeout_seconds=task.timeout_seconds,
                max_retries=task.max_retries,
                record_video=task.record_video,
                created_at=task.created_at,
                completed_at=task.completed_at,
                deleted_at=task.deleted_at,
                row_version=task.row_version,
            )
        )

    def find_by_idempotency_key(self, key: str) -> TaskRequestRecord | None:
        row = self._session.scalar(
            select(TaskRequestRow).where(TaskRequestRow.idempotency_key == key)
        )
        return _task_record(row) if row else None

    def get(self, task_id: UUID, *, for_update: bool = False) -> TaskRequestRecord | None:
        statement = select(TaskRequestRow).where(
            TaskRequestRow.id == task_id,
            TaskRequestRow.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _task_record(row) if row else None

    def get_for_reconciliation(self, task_id: UUID) -> TaskRequestRecord | None:
        # An immutable terminal report must remain replayable after task history is hidden.
        row = self._session.scalar(
            select(TaskRequestRow).where(TaskRequestRow.id == task_id).with_for_update()
        )
        return _task_record(row) if row else None

    def get_created_task(self, task_id: UUID) -> CreatedTask | None:
        task = self.get(task_id)
        if task is None:
            return None
        schedule_row = self._session.scalar(
            select(TaskScheduleRow).where(TaskScheduleRow.task_request_id == task_id)
        )
        occurrence_rows = self._session.scalars(
            select(PlanOccurrenceRow)
            .where(PlanOccurrenceRow.task_request_id == task_id)
            .order_by(PlanOccurrenceRow.ordinal)
        ).all()
        return CreatedTask(
            task=task,
            schedule=_schedule_record(schedule_row) if schedule_row else None,
            occurrences=tuple(_occurrence_record(row) for row in occurrence_rows),
        )

    def set_lifecycle(
        self,
        task_id: UUID,
        expected_version: int,
        status: str,
        completed_at: datetime | None,
    ) -> TaskRequestRecord | None:
        row = self._session.execute(
            update(TaskRequestRow)
            .where(
                TaskRequestRow.id == task_id,
                TaskRequestRow.row_version == expected_version,
            )
            .values(
                lifecycle_status=status,
                completed_at=completed_at,
                row_version=TaskRequestRow.row_version + 1,
            )
            .returning(TaskRequestRow)
        ).scalar_one_or_none()
        return _task_record(row) if row else None

    def list_history(
        self,
        *,
        before_created_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[TaskHistorySummary]:
        _validate_page(before_created_at, before_id, limit)
        latest_execution = (
            select(
                ExecutionRow.id.label("execution_id"),
                ExecutionRow.status.label("execution_status"),
                ExecutionRow.result.label("execution_result"),
            )
            .where(ExecutionRow.task_request_id == TaskRequestRow.id)
            .order_by(ExecutionRow.created_at.desc(), ExecutionRow.id.desc())
            .limit(1)
            .lateral("latest_execution")
        )
        statement = (
            select(
                TaskRequestRow,
                TaskScheduleRow.status,
                latest_execution.c.execution_id,
                latest_execution.c.execution_status,
                latest_execution.c.execution_result,
            )
            .outerjoin(
                TaskScheduleRow,
                TaskScheduleRow.task_request_id == TaskRequestRow.id,
            )
            .outerjoin(latest_execution, true())
            .where(
                TaskRequestRow.deleted_at.is_(None),
                or_(
                    TaskRequestRow.task_type.in_((TaskType.SINGLE.value, TaskType.BATCH.value)),
                    TaskRequestRow.lifecycle_status != TaskLifecycleStatus.ACTIVE.value,
                ),
            )
            .order_by(TaskRequestRow.created_at.desc(), TaskRequestRow.id.desc())
            .limit(limit)
        )
        if before_created_at is not None and before_id is not None:
            statement = statement.where(
                or_(
                    TaskRequestRow.created_at < before_created_at,
                    and_(
                        TaskRequestRow.created_at == before_created_at,
                        TaskRequestRow.id < before_id,
                    ),
                )
            )
        rows = self._session.execute(statement).all()
        return project_lineup_history(
            self._session,
            [
                TaskHistorySummary(
                    source_module=task.source_module,
                    logical_content_id=task.logical_content_id,
                    task_id=task.id,
                    name=task.name,
                    task_type=TaskType(task.task_type),
                    lifecycle_status=TaskLifecycleStatus(task.lifecycle_status),
                    schedule_status=ScheduleStatus(schedule_status) if schedule_status else None,
                    latest_execution_id=execution_id,
                    latest_execution_status=(
                        ExecutionStatus(execution_status) if execution_status else None
                    ),
                    latest_result=ExecutionResult(execution_result) if execution_result else None,
                    record_video=task.record_video,
                    created_at=task.created_at,
                    completed_at=task.completed_at,
                    row_version=task.row_version,
                )
                for task, schedule_status, execution_id, execution_status, execution_result in rows
            ],
        )

    def soft_delete(
        self, task_id: UUID, expected_version: int, deleted_at: datetime
    ) -> TaskRequestRecord | None:
        row = self._session.execute(
            update(TaskRequestRow)
            .where(
                TaskRequestRow.id == task_id,
                TaskRequestRow.row_version == expected_version,
                TaskRequestRow.deleted_at.is_(None),
            )
            .values(
                deleted_at=deleted_at,
                row_version=TaskRequestRow.row_version + 1,
            )
            .returning(TaskRequestRow)
        ).scalar_one_or_none()
        return _task_record(row) if row else None
