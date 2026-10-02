"""Maa persistence for PostgresMaaStrategyRepository."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from al1s.adapters.postgres.maa_models import (
    MaaStrategyModuleRow,
    MaaStrategyRow,
)
from al1s.adapters.postgres.maa_repository_records import (
    _strategy_record,
)
from al1s.maa.types import (
    StrategyRecord,
    StrategyStatus,
)


class PostgresMaaStrategyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_active(self, strategy_id: UUID, *, for_update: bool = False) -> StrategyRecord | None:
        statement = select(MaaStrategyRow).where(
            MaaStrategyRow.id == strategy_id,
            MaaStrategyRow.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _strategy_record(row)

    def add(self, strategy: StrategyRecord) -> None:
        self._session.add(
            MaaStrategyRow(
                id=strategy.strategy_id,
                application_id=strategy.application_id,
                name=strategy.name,
                normalized_name=strategy.normalized_name,
                status=strategy.status.value,
                current_version_id=strategy.current_version_id,
                created_at=strategy.created_at,
                updated_at=strategy.updated_at,
                deleted_at=strategy.deleted_at,
                row_version=strategy.row_version,
            )
        )

    def update_name(
        self,
        strategy_id: UUID,
        expected_version: int,
        name: str,
        normalized_name: str,
        now: datetime,
    ) -> StrategyRecord | None:
        statement = (
            update(MaaStrategyRow)
            .where(
                MaaStrategyRow.id == strategy_id,
                MaaStrategyRow.deleted_at.is_(None),
                MaaStrategyRow.status == StrategyStatus.ACTIVE.value,
                MaaStrategyRow.row_version == expected_version,
            )
            .values(
                name=name,
                normalized_name=normalized_name,
                updated_at=now,
                row_version=MaaStrategyRow.row_version + 1,
            )
            .returning(MaaStrategyRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _strategy_record(row)

    def soft_delete(self, strategy_id: UUID, expected_version: int, now: datetime) -> bool:
        statement = (
            update(MaaStrategyRow)
            .where(
                MaaStrategyRow.id == strategy_id,
                MaaStrategyRow.deleted_at.is_(None),
                MaaStrategyRow.status == StrategyStatus.ACTIVE.value,
                MaaStrategyRow.row_version == expected_version,
            )
            .values(
                status=StrategyStatus.RETIRED.value,
                deleted_at=now,
                updated_at=now,
                row_version=MaaStrategyRow.row_version + 1,
            )
            .returning(MaaStrategyRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def list_current_using_script_version(
        self, script_version_id: UUID, *, for_update: bool = False
    ) -> list[StrategyRecord]:
        statement = (
            select(MaaStrategyRow)
            .join(
                MaaStrategyModuleRow,
                MaaStrategyModuleRow.strategy_version_id == MaaStrategyRow.current_version_id,
            )
            .where(
                MaaStrategyRow.deleted_at.is_(None),
                MaaStrategyRow.status == StrategyStatus.ACTIVE.value,
                MaaStrategyModuleRow.script_version_id == script_version_id,
            )
            .order_by(MaaStrategyRow.id)
        )
        if for_update:
            statement = statement.with_for_update(of=MaaStrategyRow)
        return [
            _strategy_record(row)
            for row in self._session.execute(statement).scalars().unique().all()
        ]

    def set_current_version(
        self,
        strategy_id: UUID,
        expected_version: int,
        strategy_version_id: UUID,
        now: datetime,
    ) -> StrategyRecord | None:
        statement = (
            update(MaaStrategyRow)
            .where(
                MaaStrategyRow.id == strategy_id,
                MaaStrategyRow.deleted_at.is_(None),
                MaaStrategyRow.status == StrategyStatus.ACTIVE.value,
                MaaStrategyRow.row_version == expected_version,
            )
            .values(
                current_version_id=strategy_version_id,
                updated_at=now,
                row_version=MaaStrategyRow.row_version + 1,
            )
            .returning(MaaStrategyRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _strategy_record(row)

    def set_current_versions(
        self,
        updates: Sequence[tuple[UUID, int, UUID]],
        now: datetime,
    ) -> dict[UUID, StrategyRecord]:
        if not updates:
            return {}
        expected_versions = {strategy_id: expected for strategy_id, expected, _ in updates}
        version_ids = {strategy_id: version_id for strategy_id, _, version_id in updates}
        statement = (
            update(MaaStrategyRow)
            .where(
                MaaStrategyRow.id.in_(expected_versions),
                MaaStrategyRow.deleted_at.is_(None),
                MaaStrategyRow.status == StrategyStatus.ACTIVE.value,
                MaaStrategyRow.row_version == case(expected_versions, value=MaaStrategyRow.id),
            )
            .values(
                current_version_id=case(version_ids, value=MaaStrategyRow.id),
                updated_at=now,
                row_version=MaaStrategyRow.row_version + 1,
            )
            .returning(MaaStrategyRow)
        )
        rows = self._session.execute(statement).scalars().all()
        return {row.id: _strategy_record(row) for row in rows}

    def list_active(
        self,
        *,
        application_id: UUID | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[StrategyRecord]:
        statement = (
            select(MaaStrategyRow)
            .where(MaaStrategyRow.deleted_at.is_(None))
            .order_by(MaaStrategyRow.id)
            .limit(limit)
        )
        if application_id is not None:
            statement = statement.where(MaaStrategyRow.application_id == application_id)
        if after_id is not None:
            statement = statement.where(MaaStrategyRow.id > after_id)
        return [_strategy_record(row) for row in self._session.execute(statement).scalars().all()]
