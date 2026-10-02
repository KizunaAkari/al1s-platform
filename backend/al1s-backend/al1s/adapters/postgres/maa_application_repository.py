"""Maa persistence for PostgresMaaApplicationRepository."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from al1s.adapters.postgres.maa_models import (
    MaaApplicationRow,
)
from al1s.adapters.postgres.maa_repository_records import (
    _application_record,
)
from al1s.maa.types import (
    ApplicationRecord,
)


class PostgresMaaApplicationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_active(
        self, application_id: UUID, *, for_update: bool = False
    ) -> ApplicationRecord | None:
        statement = select(MaaApplicationRow).where(
            MaaApplicationRow.id == application_id,
            MaaApplicationRow.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _application_record(row)

    def find_active_by_packages(self, package_names: Sequence[str]) -> dict[str, ApplicationRecord]:
        if not package_names:
            return {}
        statement = select(MaaApplicationRow).where(
            MaaApplicationRow.package_name.in_(set(package_names)),
            MaaApplicationRow.deleted_at.is_(None),
        )
        return {
            row.package_name: _application_record(row)
            for row in self._session.execute(statement).scalars().all()
        }

    def find_import_targets(
        self, package_names: Sequence[str],
    ) -> dict[str, list[ApplicationRecord]]:
        if not package_names:
            return {}
        rows = self._session.scalars(
            select(MaaApplicationRow).where(
                MaaApplicationRow.package_name.in_(set(package_names)),
                MaaApplicationRow.deleted_at.is_(None),
            ).order_by(MaaApplicationRow.id).with_for_update()
        )
        result: dict[str, list[ApplicationRecord]] = {}
        for row in rows:
            result.setdefault(row.package_name, []).append(_application_record(row))
        return result

    def add_many(self, applications: Sequence[ApplicationRecord]) -> None:
        self._session.add_all(
            [
                MaaApplicationRow(
                    id=item.application_id,
                    package_name=item.package_name,
                    display_name=item.display_name,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    deleted_at=item.deleted_at,
                    row_version=item.row_version,
                    icon_png=item.icon_png,
                )
                for item in applications
            ]
        )

    def update_display_name(
        self,
        application_id: UUID,
        expected_version: int,
        display_name: str,
        now: datetime,
    ) -> ApplicationRecord | None:
        statement = (
            update(MaaApplicationRow)
            .where(
                MaaApplicationRow.id == application_id,
                MaaApplicationRow.deleted_at.is_(None),
                MaaApplicationRow.row_version == expected_version,
            )
            .values(
                display_name=display_name,
                updated_at=now,
                row_version=MaaApplicationRow.row_version + 1,
            )
            .returning(MaaApplicationRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _application_record(row)

    def update_icon(
        self, application_id: UUID, expected_version: int,
        icon_png: bytes | None, now: datetime,
    ) -> ApplicationRecord | None:
        statement = (
            update(MaaApplicationRow)
            .where(
                MaaApplicationRow.id == application_id,
                MaaApplicationRow.deleted_at.is_(None),
                MaaApplicationRow.row_version == expected_version,
            )
            .values(
                icon_png=icon_png, updated_at=now,
                row_version=MaaApplicationRow.row_version + 1,
            )
            .returning(MaaApplicationRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _application_record(row)

    def soft_delete(self, application_id: UUID, expected_version: int, now: datetime) -> bool:
        statement = (
            update(MaaApplicationRow)
            .where(
                MaaApplicationRow.id == application_id,
                MaaApplicationRow.deleted_at.is_(None),
                MaaApplicationRow.row_version == expected_version,
            )
            .values(
                deleted_at=now,
                updated_at=now,
                row_version=MaaApplicationRow.row_version + 1,
            )
            .returning(MaaApplicationRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def list_active(self, *, after_id: UUID | None, limit: int) -> list[ApplicationRecord]:
        statement = (
            select(MaaApplicationRow)
            .where(MaaApplicationRow.deleted_at.is_(None))
            .order_by(MaaApplicationRow.id)
            .limit(limit)
        )
        if after_id is not None:
            statement = statement.where(MaaApplicationRow.id > after_id)
        return [
            _application_record(row) for row in self._session.execute(statement).scalars().all()
        ]
