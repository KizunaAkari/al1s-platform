"""Maa persistence for PostgresMaaImportRepository."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from al1s.adapters.postgres.maa_models import (
    MaaImportBatchRow,
    MaaImportItemRow,
)
from al1s.adapters.postgres.maa_repository_records import (
    _import_item_record,
    _import_record,
)
from al1s.maa.types import (
    ImportBatchRecord,
    ImportItemRecord,
    ImportStatus,
    NewImportItem,
)


class PostgresMaaImportRepository:
    _LOCK_KEY = "al1s.maa.archive-import.finalize"

    def __init__(self, session: Session) -> None:
        self._session = session

    def acquire_finalize_lock(self) -> None:
        self._session.execute(select(func.pg_advisory_xact_lock(func.hashtext(self._LOCK_KEY))))

    def find_by_logical_hash(
        self, logical_sha256: str, *, for_update: bool = False
    ) -> ImportBatchRecord | None:
        statement = select(MaaImportBatchRow).where(
            MaaImportBatchRow.logical_sha256 == logical_sha256
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _import_record(row)

    def get(self, batch_id: UUID) -> ImportBatchRecord | None:
        statement = select(MaaImportBatchRow).where(MaaImportBatchRow.id == batch_id)
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _import_record(row)

    def list_items(
        self,
        batch_id: UUID,
        *,
        after_ordinal: int | None,
        limit: int,
    ) -> list[ImportItemRecord]:
        statement = (
            select(MaaImportItemRow)
            .where(MaaImportItemRow.batch_id == batch_id)
            .order_by(MaaImportItemRow.archive_ordinal)
            .limit(limit)
        )
        if after_ordinal is not None:
            statement = statement.where(MaaImportItemRow.archive_ordinal > after_ordinal)
        return [
            _import_item_record(row) for row in self._session.execute(statement).scalars().all()
        ]

    def add(self, batch: ImportBatchRecord) -> None:
        self._session.add(
            MaaImportBatchRow(
                id=batch.batch_id,
                logical_sha256=batch.logical_sha256,
                archive_sha256=batch.archive_sha256,
                archive_schema=batch.archive_schema,
                status=batch.status.value,
                script_count=batch.script_count,
                application_count=batch.application_count,
                resource_reference_count=batch.resource_reference_count,
                unique_resource_count=batch.unique_resource_count,
                error_code=batch.error_code,
                diagnostic=batch.diagnostic,
                created_at=batch.created_at,
                completed_at=batch.completed_at,
                row_version=batch.row_version,
            )
        )

    def restart_failed(
        self,
        batch_id: UUID,
        expected_version: int,
        archive_sha256: str,
        now: datetime,
    ) -> bool:
        statement = (
            update(MaaImportBatchRow)
            .where(
                MaaImportBatchRow.id == batch_id,
                MaaImportBatchRow.row_version == expected_version,
                MaaImportBatchRow.status.in_(
                    (ImportStatus.FAILED.value, ImportStatus.COMPLETED.value)
                ),
            )
            .values(
                archive_sha256=archive_sha256,
                status=ImportStatus.PROCESSING.value,
                error_code=None,
                diagnostic=None,
                completed_at=None,
                created_at=now,
                row_version=MaaImportBatchRow.row_version + 1,
            )
            .returning(MaaImportBatchRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def complete(
        self, batch_id: UUID, expected_version: int, completed_at: datetime,
        *, strategy_id: UUID | None = None,
    ) -> bool:
        statement = (
            update(MaaImportBatchRow)
            .where(
                MaaImportBatchRow.id == batch_id,
                MaaImportBatchRow.row_version == expected_version,
                MaaImportBatchRow.status == ImportStatus.PROCESSING.value,
            )
            .values(
                status=ImportStatus.COMPLETED.value,
                strategy_id=strategy_id,
                completed_at=completed_at,
                row_version=MaaImportBatchRow.row_version + 1,
            )
            .returning(MaaImportBatchRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def fail(
        self,
        batch_id: UUID,
        expected_version: int,
        error_code: str,
        diagnostic: str,
        completed_at: datetime,
    ) -> bool:
        statement = (
            update(MaaImportBatchRow)
            .where(
                MaaImportBatchRow.id == batch_id,
                MaaImportBatchRow.row_version == expected_version,
                MaaImportBatchRow.status == ImportStatus.PROCESSING.value,
            )
            .values(
                status=ImportStatus.FAILED.value,
                error_code=error_code,
                diagnostic=diagnostic[:512],
                completed_at=completed_at,
                row_version=MaaImportBatchRow.row_version + 1,
            )
            .returning(MaaImportBatchRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def add_items(self, items: Sequence[NewImportItem], created_at: datetime) -> None:
        if not items:
            return
        statement = insert(MaaImportItemRow).values(
            [
                {
                    "id": item.item_id,
                    "batch_id": item.batch_id,
                    "archive_ordinal": item.archive_ordinal,
                    "script_name": item.script_name,
                    "application_package": item.application_package,
                    "script_id": item.script_id,
                    "script_version_id": item.script_version_id,
                    "status": item.status.value,
                    "migration_code": item.migration_code,
                    "error_code": item.error_code,
                    "diagnostic": item.diagnostic,
                    "created_at": created_at,
                }
                for item in items
            ]
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_maa_import_items_ordinal",
            set_={
                "script_name": statement.excluded.script_name,
                "application_package": statement.excluded.application_package,
                "script_id": statement.excluded.script_id,
                "script_version_id": statement.excluded.script_version_id,
                "status": statement.excluded.status,
                "migration_code": statement.excluded.migration_code,
                "error_code": statement.excluded.error_code,
                "diagnostic": statement.excluded.diagnostic,
                "created_at": statement.excluded.created_at,
            },
        )
        self._session.execute(statement)
