"""Maa persistence for PostgresMaaStrategyVersionRepository."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from al1s.adapters.postgres.maa_models import (
    MaaStrategyModuleRow,
    MaaStrategyVersionRow,
)
from al1s.adapters.postgres.maa_repository_records import (
    _strategy_version_record,
)
from al1s.maa.types import (
    StrategyModuleRecord,
    StrategyModuleRole,
    StrategyVersionRecord,
)


class PostgresMaaStrategyVersionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, strategy_version_id: UUID) -> StrategyVersionRecord | None:
        row = self._session.get(MaaStrategyVersionRow, strategy_version_id)
        return None if row is None else _strategy_version_record(row)

    def find_by_ids(
        self, strategy_version_ids: Sequence[UUID]
    ) -> dict[UUID, StrategyVersionRecord]:
        unique_ids = set(strategy_version_ids)
        if not unique_ids:
            return {}
        rows = self._session.execute(
            select(MaaStrategyVersionRow).where(MaaStrategyVersionRow.id.in_(unique_ids))
        ).scalars()
        return {row.id: _strategy_version_record(row) for row in rows}

    def find_by_hash(self, strategy_id: UUID, manifest_hash: str) -> StrategyVersionRecord | None:
        statement = select(MaaStrategyVersionRow).where(
            MaaStrategyVersionRow.strategy_id == strategy_id,
            MaaStrategyVersionRow.manifest_hash == manifest_hash,
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _strategy_version_record(row)

    def find_by_hashes(
        self, keys: Sequence[tuple[UUID, str]]
    ) -> dict[tuple[UUID, str], StrategyVersionRecord]:
        unique_keys = set(keys)
        if not unique_keys:
            return {}
        rows = self._session.execute(
            select(MaaStrategyVersionRow).where(
                tuple_(
                    MaaStrategyVersionRow.strategy_id,
                    MaaStrategyVersionRow.manifest_hash,
                ).in_(unique_keys)
            )
        ).scalars()
        return {(row.strategy_id, row.manifest_hash): _strategy_version_record(row) for row in rows}

    def next_revision(self, strategy_id: UUID) -> int:
        statement = select(func.coalesce(func.max(MaaStrategyVersionRow.revision), 0) + 1).where(
            MaaStrategyVersionRow.strategy_id == strategy_id
        )
        return int(self._session.execute(statement).scalar_one())

    def next_revisions(self, strategy_ids: Sequence[UUID]) -> dict[UUID, int]:
        unique_ids = set(strategy_ids)
        if not unique_ids:
            return {}
        statement = (
            select(
                MaaStrategyVersionRow.strategy_id,
                func.coalesce(func.max(MaaStrategyVersionRow.revision), 0) + 1,
            )
            .where(MaaStrategyVersionRow.strategy_id.in_(unique_ids))
            .group_by(MaaStrategyVersionRow.strategy_id)
        )
        found = {
            strategy_id: int(revision)
            for strategy_id, revision in self._session.execute(statement).all()
        }
        return {strategy_id: found.get(strategy_id, 1) for strategy_id in unique_ids}

    def add(self, version: StrategyVersionRecord) -> None:
        self._session.add(
            MaaStrategyVersionRow(
                id=version.strategy_version_id,
                strategy_id=version.strategy_id,
                revision=version.revision,
                schema_version=version.schema_version,
                manifest_hash=version.manifest_hash,
                manifest=version.manifest,
                created_at=version.created_at,
            )
        )

    def add_many(self, versions: Sequence[StrategyVersionRecord]) -> None:
        self._session.add_all(
            [
                MaaStrategyVersionRow(
                    id=version.strategy_version_id,
                    strategy_id=version.strategy_id,
                    revision=version.revision,
                    schema_version=version.schema_version,
                    manifest_hash=version.manifest_hash,
                    manifest=version.manifest,
                    created_at=version.created_at,
                )
                for version in versions
            ]
        )

    def add_modules(self, modules: Sequence[StrategyModuleRecord]) -> None:
        self._session.add_all(
            [
                MaaStrategyModuleRow(
                    strategy_version_id=item.strategy_version_id,
                    position=item.position,
                    module_role=item.module_role.value,
                    script_version_id=item.script_version_id,
                    wait_after_ms=item.wait_after_ms,
                )
                for item in modules
            ]
        )

    def list_modules(self, strategy_version_id: UUID) -> list[StrategyModuleRecord]:
        statement = (
            select(MaaStrategyModuleRow)
            .where(MaaStrategyModuleRow.strategy_version_id == strategy_version_id)
            .order_by(MaaStrategyModuleRow.position)
        )
        return [
            StrategyModuleRecord(
                strategy_version_id=row.strategy_version_id,
                position=row.position,
                module_role=StrategyModuleRole(row.module_role),
                script_version_id=row.script_version_id,
                wait_after_ms=row.wait_after_ms,
            )
            for row in self._session.execute(statement).scalars().all()
        ]

    def list_modules_many(
        self, strategy_version_ids: Sequence[UUID]
    ) -> dict[UUID, list[StrategyModuleRecord]]:
        unique_ids = set(strategy_version_ids)
        if not unique_ids:
            return {}
        statement = (
            select(MaaStrategyModuleRow)
            .where(MaaStrategyModuleRow.strategy_version_id.in_(unique_ids))
            .order_by(
                MaaStrategyModuleRow.strategy_version_id,
                MaaStrategyModuleRow.position,
            )
        )
        result: dict[UUID, list[StrategyModuleRecord]] = {
            version_id: [] for version_id in unique_ids
        }
        for row in self._session.execute(statement).scalars().all():
            result[row.strategy_version_id].append(
                StrategyModuleRecord(
                    strategy_version_id=row.strategy_version_id,
                    position=row.position,
                    module_role=StrategyModuleRole(row.module_role),
                    script_version_id=row.script_version_id,
                    wait_after_ms=row.wait_after_ms,
                )
            )
        return result

    def list_for_strategy(
        self, strategy_id: UUID, *, after_revision: int | None, limit: int
    ) -> list[StrategyVersionRecord]:
        statement = (
            select(MaaStrategyVersionRow)
            .where(MaaStrategyVersionRow.strategy_id == strategy_id)
            .order_by(MaaStrategyVersionRow.revision)
            .limit(limit)
        )
        if after_revision is not None:
            statement = statement.where(MaaStrategyVersionRow.revision > after_revision)
        return [
            _strategy_version_record(row)
            for row in self._session.execute(statement).scalars().all()
        ]
