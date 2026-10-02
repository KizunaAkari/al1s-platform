from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from al1s.adapters.postgres.scheduling_models import (
    TaskRetryOriginRow,
)
from al1s.execution.scheduling_types import (
    TaskRetryOriginRecord,
)


class PostgresTaskRetryOriginRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, origin: TaskRetryOriginRecord) -> None:
        self._session.add(
            TaskRetryOriginRow(
                task_request_id=origin.task_request_id,
                source_execution_id=origin.source_execution_id,
                source_snapshot_id=origin.source_snapshot_id,
                created_at=origin.created_at,
            )
        )

    def get_by_task(self, task_id: UUID) -> TaskRetryOriginRecord | None:
        row = self._session.get(TaskRetryOriginRow, task_id)
        if row is None:
            return None
        return TaskRetryOriginRecord(
            task_request_id=row.task_request_id,
            source_execution_id=row.source_execution_id,
            source_snapshot_id=row.source_snapshot_id,
            created_at=row.created_at,
        )
