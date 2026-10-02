"""Maa persistence for PostgresMaaScriptVersionRepository."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.maa_models import (
    MaaScriptVersionBlobRow,
    MaaScriptVersionRow,
)
from al1s.adapters.postgres.maa_repository_records import (
    _version_record,
)
from al1s.maa.types import (
    ScriptBlobReference,
    ScriptVersionRecord,
)


class PostgresMaaScriptVersionRepository:
    def next_import_revisions(self, script_ids: Sequence[UUID]) -> dict[UUID, int]:
        if not script_ids:
            return {}
        rows = self._session.execute(
            select(MaaScriptVersionRow.script_id, func.max(MaaScriptVersionRow.revision) + 1)
            .where(MaaScriptVersionRow.script_id.in_(set(script_ids)))
            .group_by(MaaScriptVersionRow.script_id)
        )
        return {identity: revision for identity, revision in rows}

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, script_version_id: UUID) -> ScriptVersionRecord | None:
        row = self._session.get(MaaScriptVersionRow, script_version_id)
        return None if row is None else _version_record(row)

    def script_ids_for_versions(self, version_ids: Sequence[UUID]) -> dict[UUID, UUID]:
        if not version_ids:
            return {}
        rows = self._session.execute(
            select(MaaScriptVersionRow.id, MaaScriptVersionRow.script_id).where(
                MaaScriptVersionRow.id.in_(set(version_ids))
            )
        )
        return {version_id: script_id for version_id, script_id in rows}

    def find_by_ids(self, script_version_ids: Sequence[UUID]) -> dict[UUID, ScriptVersionRecord]:
        unique_ids = set(script_version_ids)
        if not unique_ids:
            return {}
        rows = self._session.execute(
            select(MaaScriptVersionRow).where(MaaScriptVersionRow.id.in_(unique_ids))
        ).scalars()
        return {row.id: _version_record(row) for row in rows}

    def find_by_hash(self, script_id: UUID, manifest_hash: str) -> ScriptVersionRecord | None:
        statement = (
            select(MaaScriptVersionRow)
            .where(
                MaaScriptVersionRow.script_id == script_id,
                MaaScriptVersionRow.manifest_hash == manifest_hash,
            )
            .order_by(MaaScriptVersionRow.revision.desc())
            .limit(1)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _version_record(row)

    def next_revision(self, script_id: UUID) -> int:
        statement = select(func.coalesce(func.max(MaaScriptVersionRow.revision), 0) + 1).where(
            MaaScriptVersionRow.script_id == script_id
        )
        return int(self._session.execute(statement).scalar_one())

    def add_many(self, versions: Sequence[ScriptVersionRecord]) -> None:
        self._session.add_all(
            [
                MaaScriptVersionRow(
                    id=item.script_version_id,
                    script_id=item.script_id,
                    revision=item.revision,
                    schema_version=item.schema_version,
                    manifest_hash=item.manifest_hash,
                    manifest=item.manifest,
                    created_at=item.created_at,
                )
                for item in versions
            ]
        )

    def add_blob_references(self, references: Sequence[ScriptBlobReference]) -> None:
        self._session.add_all(
            [
                MaaScriptVersionBlobRow(
                    script_version_id=item.script_version_id,
                    blob_id=item.blob_id,
                    json_pointer=item.json_pointer,
                    resource_role=item.resource_role,
                    ordinal=item.ordinal,
                )
                for item in references
            ]
        )

    def has_blob_reference(self, script_version_id: UUID, blob_id: UUID) -> bool:
        return bool(
            self._session.scalar(
                select(
                    select(MaaScriptVersionBlobRow.blob_id)
                    .where(
                        MaaScriptVersionBlobRow.script_version_id == script_version_id,
                        MaaScriptVersionBlobRow.blob_id == blob_id,
                    )
                    .exists()
                )
            )
        )

    def list_blob_references(self, script_version_id: UUID) -> list[ScriptBlobReference]:
        statement = (
            select(MaaScriptVersionBlobRow)
            .where(MaaScriptVersionBlobRow.script_version_id == script_version_id)
            .order_by(MaaScriptVersionBlobRow.ordinal, MaaScriptVersionBlobRow.json_pointer)
        )
        return [
            ScriptBlobReference(
                script_version_id=row.script_version_id,
                blob_id=row.blob_id,
                json_pointer=row.json_pointer,
                resource_role=row.resource_role,
                ordinal=row.ordinal,
            )
            for row in self._session.execute(statement).scalars().all()
        ]

    def list_blob_references_many(
        self, script_version_ids: Sequence[UUID]
    ) -> dict[UUID, list[ScriptBlobReference]]:
        unique_ids = set(script_version_ids)
        if not unique_ids:
            return {}
        statement = (
            select(MaaScriptVersionBlobRow)
            .where(MaaScriptVersionBlobRow.script_version_id.in_(unique_ids))
            .order_by(
                MaaScriptVersionBlobRow.script_version_id,
                MaaScriptVersionBlobRow.ordinal,
                MaaScriptVersionBlobRow.json_pointer,
            )
        )
        result: dict[UUID, list[ScriptBlobReference]] = {
            version_id: [] for version_id in unique_ids
        }
        for row in self._session.execute(statement).scalars().all():
            result[row.script_version_id].append(
                ScriptBlobReference(
                    script_version_id=row.script_version_id,
                    blob_id=row.blob_id,
                    json_pointer=row.json_pointer,
                    resource_role=row.resource_role,
                    ordinal=row.ordinal,
                )
            )
        return result

    def list_for_script(
        self,
        script_id: UUID,
        *,
        after_revision: int | None,
        limit: int,
        newest_first: bool = False,
    ) -> list[ScriptVersionRecord]:
        statement = (
            select(MaaScriptVersionRow)
            .where(MaaScriptVersionRow.script_id == script_id)
        )
        if after_revision is not None:
            statement = statement.where(
                MaaScriptVersionRow.revision < after_revision
                if newest_first
                else MaaScriptVersionRow.revision > after_revision
            )
        statement = statement.order_by(
            MaaScriptVersionRow.revision.desc() if newest_first else MaaScriptVersionRow.revision
        ).limit(limit)
        return [_version_record(row) for row in self._session.execute(statement).scalars().all()]
