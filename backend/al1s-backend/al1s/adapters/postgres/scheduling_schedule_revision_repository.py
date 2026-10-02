from __future__ import annotations

from collections.abc import Sequence
from datetime import time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.scheduling_models import (
    TaskScheduleRevisionRow,
    TaskScheduleRevisionTimeRow,
)
from al1s.adapters.postgres.scheduling_repository_records import (
    _schedule_revision_record,
)
from al1s.execution.scheduling_types import (
    ScheduleRevisionRecord,
    ScheduleRevisionTimeRecord,
    TaskScheduleRecord,
)


class PostgresScheduleRevisionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        revision: ScheduleRevisionRecord,
        times: Sequence[ScheduleRevisionTimeRecord],
    ) -> None:
        self._session.add(
            TaskScheduleRevisionRow(
                id=revision.schedule_revision_id,
                schedule_id=revision.schedule_id,
                revision=revision.revision,
                timezone=revision.timezone,
                start_date=revision.start_date,
                end_date=revision.end_date,
                created_at=revision.created_at,
            )
        )
        self._session.flush()
        self._session.add_all(
            [
                TaskScheduleRevisionTimeRow(
                    id=item.schedule_revision_time_id,
                    schedule_revision_id=item.schedule_revision_id,
                    ordinal=item.ordinal,
                    local_time=item.local_time,
                )
                for item in times
            ]
        )

    def get_current(self, schedule: TaskScheduleRecord) -> ScheduleRevisionRecord | None:
        if schedule.current_revision is None:
            return None
        row = self._session.scalar(
            select(TaskScheduleRevisionRow).where(
                TaskScheduleRevisionRow.schedule_id == schedule.schedule_id,
                TaskScheduleRevisionRow.revision == schedule.current_revision,
            )
        )
        return _schedule_revision_record(row) if row else None

    def list_times(self, schedule_revision_id: UUID) -> tuple[time, ...]:
        return tuple(
            self._session.scalars(
                select(TaskScheduleRevisionTimeRow.local_time)
                .where(TaskScheduleRevisionTimeRow.schedule_revision_id == schedule_revision_id)
                .order_by(TaskScheduleRevisionTimeRow.ordinal)
            ).all()
        )
