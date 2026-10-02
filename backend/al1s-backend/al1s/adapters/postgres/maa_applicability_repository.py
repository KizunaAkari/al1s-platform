"""Phone-level applicability; never infer script rights from a Linux terminal."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from al1s.adapters.postgres.execution_models import TargetDeviceRow
from al1s.adapters.postgres.maa_models import MaaApplicationDeviceRow
from al1s.adapters.postgres.scheduling_models import ExecutionRow, TaskRequestRow
from al1s.maa.types import ApplicationDeviceRecord


def _record(row: MaaApplicationDeviceRow) -> ApplicationDeviceRecord:
    return ApplicationDeviceRecord(
        binding_id=row.id,
        application_id=row.application_id,
        device_id=row.device_id,
        package_name=row.package_name,
        created_at=row.created_at,
    )


class PostgresMaaApplicationDeviceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_active_device(self, device_id: UUID) -> bool:
        return (
            self._session.scalar(
                select(TargetDeviceRow.id)
                .where(
                    TargetDeviceRow.id == device_id,
                    TargetDeviceRow.deleted_at.is_(None),
                )
                .with_for_update()
            )
            is not None
        )

    def for_phone_package(
        self, device_id: UUID, package_name: str
    ) -> ApplicationDeviceRecord | None:
        row = self._session.scalar(
            select(MaaApplicationDeviceRow).where(
                MaaApplicationDeviceRow.device_id == device_id,
                MaaApplicationDeviceRow.package_name == package_name,
                MaaApplicationDeviceRow.deleted_at.is_(None),
            )
        )
        return _record(row) if row is not None else None

    def for_application(self, application_id: UUID) -> list[ApplicationDeviceRecord]:
        rows = self._session.scalars(
            select(MaaApplicationDeviceRow)
            .where(
                MaaApplicationDeviceRow.application_id == application_id,
                MaaApplicationDeviceRow.deleted_at.is_(None),
            )
            .order_by(MaaApplicationDeviceRow.device_id)
        ).all()
        return [_record(row) for row in rows]

    def for_device(self, device_id: UUID, limit: int = 201) -> list[ApplicationDeviceRecord]:
        rows = self._session.scalars(
            select(MaaApplicationDeviceRow)
            .where(
                MaaApplicationDeviceRow.device_id == device_id,
                MaaApplicationDeviceRow.deleted_at.is_(None),
            )
            .order_by(MaaApplicationDeviceRow.application_id)
            .limit(limit)
        ).all()
        return [_record(row) for row in rows]

    def add(self, binding: ApplicationDeviceRecord) -> None:
        self._session.add(
            MaaApplicationDeviceRow(
                id=binding.binding_id,
                application_id=binding.application_id,
                device_id=binding.device_id,
                package_name=binding.package_name,
                created_at=binding.created_at,
                deleted_at=None,
            )
        )

    def remove(self, application_id: UUID, device_id: UUID, now: datetime) -> bool:
        result = self._session.execute(
            update(MaaApplicationDeviceRow)
            .where(
                MaaApplicationDeviceRow.application_id == application_id,
                MaaApplicationDeviceRow.device_id == device_id,
                MaaApplicationDeviceRow.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )
        return bool(cast(CursorResult[Any], result).rowcount)

    def active_maa_tasks(self, device_id: UUID, limit: int = 51) -> list[UUID]:
        return list(
            self._session.scalars(
                select(TaskRequestRow.id)
                .join(
                    ExecutionRow,
                    ExecutionRow.task_request_id == TaskRequestRow.id,
                )
                .where(
                    TaskRequestRow.requested_target_device_id == device_id,
                    TaskRequestRow.source_module == "maa",
                    TaskRequestRow.lifecycle_status == "active",
                    TaskRequestRow.deleted_at.is_(None),
                    ExecutionRow.status.in_(("waiting", "queued", "running")),
                )
                .distinct()
                .order_by(TaskRequestRow.id)
                .limit(limit)
            ).all()
        )

    def active_content_tasks(
        self, logical_content_ids: Sequence[str], limit: int = 51
    ) -> list[UUID]:
        if not logical_content_ids:
            return []
        return list(
            self._session.scalars(
                select(TaskRequestRow.id)
                .where(
                    TaskRequestRow.source_module == "maa",
                    TaskRequestRow.logical_content_id.in_(logical_content_ids),
                    TaskRequestRow.lifecycle_status == "active",
                    TaskRequestRow.deleted_at.is_(None),
                )
                .order_by(TaskRequestRow.id)
                .limit(limit)
            ).all()
        )
