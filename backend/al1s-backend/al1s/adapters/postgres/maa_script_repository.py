"""Maa persistence for PostgresMaaScriptRepository."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import case, func, select, tuple_, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from al1s.adapters.postgres.maa_models import (
    MaaApplicationDeviceRow,
    MaaApplicationRow,
    MaaScriptRow,
)
from al1s.adapters.postgres.maa_repository_records import (
    _script_record,
)
from al1s.maa.errors import MaaDomainError
from al1s.maa.types import (
    ScriptRecord,
    ScriptStatus,
    ScriptType,
)


class PostgresMaaScriptRepository:
    def list_editor_drafts(
        self,
        *,
        device_id: UUID,
        after_id: UUID | None,
        limit: int,
    ) -> list[ScriptRecord]:
        statement = (
            select(MaaScriptRow)
            .join(
                MaaApplicationDeviceRow,
                MaaApplicationDeviceRow.application_id == MaaScriptRow.application_id,
            )
            .join(MaaApplicationRow, MaaApplicationRow.id == MaaScriptRow.application_id)
            .where(
                MaaApplicationDeviceRow.device_id == device_id,
                MaaApplicationDeviceRow.deleted_at.is_(None),
                MaaApplicationRow.deleted_at.is_(None),
                MaaScriptRow.deleted_at.is_(None),
                MaaScriptRow.current_version_id.is_(None),
                MaaScriptRow.candidate_version_id.is_(None),
                MaaScriptRow.script_type.in_(
                    [
                        ScriptType.MODULE_START.value,
                        ScriptType.MODULE_PROCESS.value,
                        ScriptType.MODULE_END.value,
                    ]
                ),
            )
            .order_by(MaaScriptRow.id)
            .limit(limit)
        )
        if after_id is not None:
            statement = statement.where(MaaScriptRow.id > after_id)
        return [_script_record(row) for row in self._session.scalars(statement)]

    @staticmethod
    def _saved_library_scope(application_id: UUID) -> tuple[ColumnElement[bool], ...]:
        return (
            MaaScriptRow.application_id == application_id,
            MaaScriptRow.deleted_at.is_(None),
            MaaScriptRow.status == ScriptStatus.ACTIVE.value,
            MaaScriptRow.current_version_id.is_not(None),
            MaaScriptRow.script_type.in_(
                [
                    ScriptType.MODULE_START.value,
                    ScriptType.MODULE_PROCESS.value,
                    ScriptType.MODULE_END.value,
                ]
            ),
        )

    def list_saved_for_library(
        self,
        *,
        application_id: UUID,
        after_id: UUID | None,
        limit: int,
    ) -> list[ScriptRecord]:
        scope = self._saved_library_scope(application_id)
        statement = (
            select(MaaScriptRow)
            .where(*scope)
            .order_by(
                MaaScriptRow.display_order,
                MaaScriptRow.id,
            )
            .limit(limit)
        )
        if after_id is not None:
            anchor = self._session.execute(
                select(MaaScriptRow.display_order, MaaScriptRow.id).where(
                    *scope,
                    MaaScriptRow.id == after_id,
                )
            ).one_or_none()
            if anchor is None:
                raise MaaDomainError("script_order_changed", "脚本列表已变化, 请刷新后重试。", 409)
            statement = statement.where(
                tuple_(MaaScriptRow.display_order, MaaScriptRow.id) > anchor
            )
        return [_script_record(row) for row in self._session.scalars(statement)]

    def move_display_order(
        self,
        *,
        application_id: UUID,
        script_id: UUID,
        target_id: UUID,
        placement: str,
    ) -> bool:
        scope = self._saved_library_scope(application_id)
        rows = self._session.scalars(
            select(MaaScriptRow)
            .where(*scope, MaaScriptRow.id.in_([script_id, target_id]))
            .order_by(MaaScriptRow.id)
            .with_for_update()
        ).all()
        by_id = {row.id: row for row in rows}
        if len(by_id) != 2:
            return False
        target = by_id[target_id]
        target_key = (target.display_order, target.id)
        cursor = tuple_(MaaScriptRow.display_order, MaaScriptRow.id)
        if placement == "before":
            neighbor = self._session.scalar(
                select(MaaScriptRow.display_order)
                .where(
                    *scope,
                    MaaScriptRow.id != script_id,
                    cursor < target_key,
                )
                .order_by(MaaScriptRow.display_order.desc(), MaaScriptRow.id.desc())
                .limit(1)
            )
            rank = (
                target.display_order - 1
                if neighbor is None
                else (neighbor + target.display_order) / Decimal(2)
            )
        else:
            neighbor = self._session.scalar(
                select(MaaScriptRow.display_order)
                .where(
                    *scope,
                    MaaScriptRow.id != script_id,
                    cursor > target_key,
                )
                .order_by(MaaScriptRow.display_order, MaaScriptRow.id)
                .limit(1)
            )
            rank = (
                Decimal(
                    self._session.execute(
                        select(func.nextval("maa_script_display_order_seq"))
                    ).scalar_one()
                )
                if neighbor is None
                else (target.display_order + neighbor) / Decimal(2)
            )
        self._session.execute(
            update(MaaScriptRow).where(MaaScriptRow.id == script_id).values(display_order=rank)
        )
        return True

    def count_saved_by_applications(self, application_ids: Sequence[UUID]) -> dict[UUID, int]:
        if not application_ids:
            return {}
        statement = (
            select(MaaScriptRow.application_id, func.count(MaaScriptRow.id))
            .where(
                MaaScriptRow.application_id.in_(set(application_ids)),
                MaaScriptRow.deleted_at.is_(None),
                MaaScriptRow.status == ScriptStatus.ACTIVE.value,
                MaaScriptRow.current_version_id.is_not(None),
                MaaScriptRow.script_type.in_(
                    [
                        ScriptType.MODULE_START.value,
                        ScriptType.MODULE_PROCESS.value,
                        ScriptType.MODULE_END.value,
                    ]
                ),
            )
            .group_by(MaaScriptRow.application_id)
        )
        return {application_id: count for application_id, count in self._session.execute(statement)}

    def find_import_boundaries(
        self, application_ids: Sequence[UUID]
    ) -> dict[tuple[UUID, ScriptType], ScriptRecord]:
        if not application_ids:
            return {}
        rows = self._session.scalars(
            select(MaaScriptRow)
            .where(
                MaaScriptRow.application_id.in_(set(application_ids)),
                MaaScriptRow.script_type.in_(
                    [ScriptType.MODULE_START.value, ScriptType.MODULE_END.value]
                ),
                MaaScriptRow.deleted_at.is_(None),
            )
            .order_by(MaaScriptRow.id)
            .with_for_update()
        )
        return {(r.application_id, ScriptType(r.script_type)): _script_record(r) for r in rows}

    def replace_imported_versions(
        self, updates: Sequence[tuple[UUID, int, UUID]], now: datetime
    ) -> int:
        if not updates:
            return 0
        versions = {identity: version for identity, _, version in updates}
        statement = (
            update(MaaScriptRow)
            .where(
                tuple_(MaaScriptRow.id, MaaScriptRow.row_version).in_(
                    [(identity, expected) for identity, expected, _ in updates]
                ),
                MaaScriptRow.deleted_at.is_(None),
            )
            .values(
                current_version_id=case(versions, value=MaaScriptRow.id),
                candidate_version_id=None,
                status=ScriptStatus.ACTIVE.value,
                updated_at=now,
                row_version=MaaScriptRow.row_version + 1,
            )
            .returning(MaaScriptRow.id)
        )
        return len(self._session.scalars(statement).all())

    def list_referrers(
        self, script_id: UUID, *, after_id: UUID | None, limit: int
    ) -> list[dict[str, object]]:
        from al1s.adapters.postgres.maa_reference_queries import script_referrers

        return script_referrers(self._session, script_id, after_id=after_id, limit=limit)

    def has_strategy_referrers(self, script_id: UUID) -> bool:
        from al1s.adapters.postgres.maa_reference_queries import strategy_referrers

        return strategy_referrers(self._session, script_id)

    def soft_delete(self, script_id: UUID, expected_version: int, now: datetime) -> bool:
        statement = (
            update(MaaScriptRow)
            .where(
                MaaScriptRow.id == script_id,
                MaaScriptRow.row_version == expected_version,
                MaaScriptRow.deleted_at.is_(None),
            )
            .values(deleted_at=now, updated_at=now, row_version=MaaScriptRow.row_version + 1)
            .returning(MaaScriptRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def __init__(self, session: Session) -> None:
        self._session = session

    def update_name(
        self,
        script_id: UUID,
        expected_version: int,
        name: str,
        normalized_name: str,
        now: datetime,
    ) -> ScriptRecord | None:
        statement = (
            update(MaaScriptRow)
            .where(
                MaaScriptRow.id == script_id,
                MaaScriptRow.deleted_at.is_(None),
                MaaScriptRow.row_version == expected_version,
            )
            .values(
                name=name,
                normalized_name=normalized_name,
                updated_at=now,
                row_version=MaaScriptRow.row_version + 1,
            )
            .returning(MaaScriptRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _script_record(row)

    def get_active(self, script_id: UUID, *, for_update: bool = False) -> ScriptRecord | None:
        statement = select(MaaScriptRow).where(
            MaaScriptRow.id == script_id,
            MaaScriptRow.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _script_record(row)

    def find_active_by_keys(
        self,
        keys: Sequence[tuple[UUID, str]],
        *,
        for_update: bool = False,
    ) -> dict[tuple[UUID, str], ScriptRecord]:
        if not keys:
            return {}
        statement = select(MaaScriptRow).where(
            tuple_(MaaScriptRow.application_id, MaaScriptRow.normalized_name).in_(set(keys)),
            MaaScriptRow.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.order_by(MaaScriptRow.id).with_for_update()
        return {
            (row.application_id, row.normalized_name): _script_record(row)
            for row in self._session.execute(statement).scalars().all()
        }

    def find_active_by_ids(self, script_ids: Sequence[UUID]) -> dict[UUID, ScriptRecord]:
        if not script_ids:
            return {}
        statement = select(MaaScriptRow).where(
            MaaScriptRow.id.in_(set(script_ids)),
            MaaScriptRow.deleted_at.is_(None),
        )
        return {
            row.id: _script_record(row) for row in self._session.execute(statement).scalars().all()
        }

    def find_active_by_types(
        self, application_id: UUID, script_types: Sequence[ScriptType]
    ) -> dict[ScriptType, ScriptRecord]:
        values = {item.value for item in script_types}
        if not values:
            return {}
        statement = select(MaaScriptRow).where(
            MaaScriptRow.application_id == application_id,
            MaaScriptRow.script_type.in_(values),
            MaaScriptRow.deleted_at.is_(None),
        )
        return {
            ScriptType(row.script_type): _script_record(row)
            for row in self._session.execute(statement).scalars().all()
        }

    def add_many(self, scripts: Sequence[ScriptRecord]) -> None:
        self._session.add_all(
            [
                MaaScriptRow(
                    id=item.script_id,
                    application_id=item.application_id,
                    name=item.name,
                    normalized_name=item.normalized_name,
                    script_type=item.script_type.value,
                    status=item.status.value,
                    current_version_id=item.current_version_id,
                    candidate_version_id=item.candidate_version_id,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    deleted_at=item.deleted_at,
                    row_version=item.row_version,
                )
                for item in scripts
            ]
        )

    def set_initial_candidates(self, versions_by_script: dict[UUID, UUID], now: datetime) -> int:
        if not versions_by_script:
            return 0
        statement = (
            update(MaaScriptRow)
            .where(
                MaaScriptRow.id.in_(versions_by_script),
                MaaScriptRow.candidate_version_id.is_(None),
                MaaScriptRow.row_version == 1,
            )
            .values(
                candidate_version_id=case(versions_by_script, value=MaaScriptRow.id),
                updated_at=now,
                row_version=MaaScriptRow.row_version + 1,
            )
            .returning(MaaScriptRow.id)
        )
        return len(self._session.execute(statement).scalars().all())

    def activate_imported_versions(
        self, versions_by_script: dict[UUID, UUID], now: datetime
    ) -> int:
        if not versions_by_script:
            return 0
        statement = (
            update(MaaScriptRow)
            .where(
                MaaScriptRow.id.in_(versions_by_script),
                MaaScriptRow.current_version_id.is_(None),
                MaaScriptRow.candidate_version_id.is_(None),
                MaaScriptRow.deleted_at.is_(None),
                MaaScriptRow.row_version == 1,
            )
            .values(
                current_version_id=case(versions_by_script, value=MaaScriptRow.id),
                status=ScriptStatus.ACTIVE.value,
                updated_at=now,
                row_version=MaaScriptRow.row_version + 1,
            )
            .returning(MaaScriptRow.id)
        )
        return len(self._session.execute(statement).scalars().all())

    def set_candidate(
        self,
        script_id: UUID,
        expected_version: int,
        candidate_version_id: UUID,
        now: datetime,
    ) -> ScriptRecord | None:
        statement = (
            update(MaaScriptRow)
            .where(
                MaaScriptRow.id == script_id,
                MaaScriptRow.deleted_at.is_(None),
                MaaScriptRow.status != ScriptStatus.RETIRED.value,
                MaaScriptRow.row_version == expected_version,
            )
            .values(
                candidate_version_id=candidate_version_id,
                updated_at=now,
                row_version=MaaScriptRow.row_version + 1,
            )
            .returning(MaaScriptRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _script_record(row)

    def activate_saved_version(
        self,
        script_id: UUID,
        expected_version: int,
        saved_version_id: UUID,
        now: datetime,
    ) -> ScriptRecord | None:
        statement = (
            update(MaaScriptRow)
            .where(
                MaaScriptRow.id == script_id,
                MaaScriptRow.deleted_at.is_(None),
                MaaScriptRow.status != ScriptStatus.RETIRED.value,
                MaaScriptRow.row_version == expected_version,
                MaaScriptRow.candidate_version_id.is_(None),
            )
            .values(
                current_version_id=saved_version_id,
                candidate_version_id=None,
                status=ScriptStatus.ACTIVE.value,
                updated_at=now,
                row_version=MaaScriptRow.row_version + 1,
            )
            .returning(MaaScriptRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _script_record(row)

    def publish_candidate(
        self,
        script_id: UUID,
        expected_version: int,
        candidate_version_id: UUID,
        now: datetime,
    ) -> ScriptRecord | None:
        statement = (
            update(MaaScriptRow)
            .where(
                MaaScriptRow.id == script_id,
                MaaScriptRow.deleted_at.is_(None),
                MaaScriptRow.status != ScriptStatus.RETIRED.value,
                MaaScriptRow.row_version == expected_version,
                MaaScriptRow.candidate_version_id == candidate_version_id,
            )
            .values(
                current_version_id=candidate_version_id,
                candidate_version_id=None,
                status=ScriptStatus.ACTIVE.value,
                updated_at=now,
                row_version=MaaScriptRow.row_version + 1,
            )
            .returning(MaaScriptRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _script_record(row)

    def move_to_application(
        self,
        script_id: UUID,
        expected_version: int,
        application_id: UUID,
        now: datetime,
    ) -> ScriptRecord | None:
        statement = (
            update(MaaScriptRow)
            .where(
                MaaScriptRow.id == script_id,
                MaaScriptRow.deleted_at.is_(None),
                MaaScriptRow.status != ScriptStatus.RETIRED.value,
                MaaScriptRow.row_version == expected_version,
            )
            .values(
                application_id=application_id,
                status=ScriptStatus.VALIDATION_PENDING.value,
                candidate_version_id=None,
                updated_at=now,
                row_version=MaaScriptRow.row_version + 1,
            )
            .returning(MaaScriptRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _script_record(row)

    def list_active(
        self,
        *,
        application_id: UUID | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[ScriptRecord]:
        statement = (
            select(MaaScriptRow)
            .where(MaaScriptRow.deleted_at.is_(None))
            .order_by(MaaScriptRow.id)
            .limit(limit)
        )
        if application_id is not None:
            statement = statement.where(MaaScriptRow.application_id == application_id)
        if after_id is not None:
            statement = statement.where(MaaScriptRow.id > after_id)
        return [_script_record(row) for row in self._session.execute(statement).scalars().all()]
