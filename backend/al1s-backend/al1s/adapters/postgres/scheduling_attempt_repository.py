from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from al1s.adapters.postgres.delivery_models import TaskPackageRow
from al1s.adapters.postgres.scheduling_models import (
    ExecutionAttemptRow,
    ExecutionRow,
)
from al1s.adapters.postgres.scheduling_repository_records import (
    _attempt_record,
)
from al1s.execution.scheduling_types import (
    AttemptStatus,
    ExecutionAttemptRecord,
    ExecutionStatus,
)


class PostgresAttemptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, attempt: ExecutionAttemptRecord) -> None:
        self._session.add(
            ExecutionAttemptRow(
                id=attempt.attempt_id,
                execution_id=attempt.execution_id,
                attempt_no=attempt.attempt_no,
                status=attempt.status.value,
                result=attempt.result.value if attempt.result else None,
                available_at=attempt.available_at,
                enqueued_at=attempt.enqueued_at,
                started_at=attempt.started_at,
                ended_at=attempt.ended_at,
                error_code=attempt.error_code,
                retryable=attempt.retryable,
                failure_phase=attempt.failure_phase.value if attempt.failure_phase else None,
                lease_id=attempt.lease_id,
                row_version=attempt.row_version,
            )
        )

    def get_for_update(self, attempt_id: UUID) -> ExecutionAttemptRecord | None:
        row = self._session.scalar(
            select(ExecutionAttemptRow)
            .where(ExecutionAttemptRow.id == attempt_id)
            .with_for_update()
        )
        return _attempt_record(row) if row else None

    def list_by_execution(self, execution_id: UUID, *, limit: int) -> list[ExecutionAttemptRecord]:
        if not 1 <= limit <= 101:
            raise ValueError("limit must be between 1 and 101")
        rows = self._session.scalars(
            select(ExecutionAttemptRow)
            .where(ExecutionAttemptRow.execution_id == execution_id)
            .order_by(ExecutionAttemptRow.attempt_no, ExecutionAttemptRow.id)
            .limit(limit)
        ).all()
        return [_attempt_record(row) for row in rows]

    def get_active_for_execution(self, execution_id: UUID) -> ExecutionAttemptRecord | None:
        row = self._session.scalar(
            select(ExecutionAttemptRow)
            .where(
                ExecutionAttemptRow.execution_id == execution_id,
                ExecutionAttemptRow.status.in_(
                    (AttemptStatus.QUEUED.value, AttemptStatus.RUNNING.value)
                ),
            )
            .with_for_update()
        )
        return _attempt_record(row) if row else None

    def is_next_for_resources(
        self,
        attempt_id: UUID,
        *,
        terminal_id: UUID,
        target_device_id: UUID | None,
        now: datetime,
    ) -> bool:
        resource_filter = ExecutionRow.terminal_id == terminal_id
        if target_device_id is not None:
            resource_filter = or_(
                resource_filter,
                ExecutionRow.target_device_id == target_device_id,
            )
        next_id = self._session.scalar(
            select(ExecutionAttemptRow.id)
            .join(ExecutionRow, ExecutionRow.id == ExecutionAttemptRow.execution_id)
            .join(TaskPackageRow, TaskPackageRow.attempt_id == ExecutionAttemptRow.id)
            .where(
                ExecutionAttemptRow.status == AttemptStatus.QUEUED.value,
                ExecutionAttemptRow.available_at <= now,
                ExecutionRow.status == ExecutionStatus.QUEUED.value,
                TaskPackageRow.status == "accepted",
                resource_filter,
            )
            .order_by(
                ExecutionAttemptRow.available_at,
                ExecutionAttemptRow.enqueued_at,
                ExecutionAttemptRow.id,
            )
            .limit(1)
        )
        return next_id == attempt_id

    def set_status(
        self,
        attempt_id: UUID,
        expected_version: int,
        status: str,
        *,
        result: str | None,
        now: datetime,
        error_code: str | None,
        retryable: bool | None,
        failure_phase: str | None,
        lease_id: UUID | None = None,
    ) -> ExecutionAttemptRecord | None:
        values: dict[str, object] = {
            "status": status,
            "result": result,
            "error_code": error_code,
            "retryable": retryable,
            "failure_phase": failure_phase,
            "row_version": ExecutionAttemptRow.row_version + 1,
        }
        if lease_id is not None:
            values["lease_id"] = lease_id
        if status == AttemptStatus.RUNNING.value:
            values["started_at"] = now
        elif status in {
            AttemptStatus.ENDED.value,
            AttemptStatus.CANCELLED.value,
            AttemptStatus.TIMED_OUT.value,
        }:
            values["ended_at"] = now
        row = self._session.execute(
            update(ExecutionAttemptRow)
            .where(
                ExecutionAttemptRow.id == attempt_id,
                ExecutionAttemptRow.row_version == expected_version,
            )
            .values(**values)
            .returning(ExecutionAttemptRow)
        ).scalar_one_or_none()
        return _attempt_record(row) if row else None
