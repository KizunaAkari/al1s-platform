from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from al1s.adapters.postgres.meme_base import MemeRepositoryBase, fingerprint
from al1s.adapters.postgres.meme_models import (
    MemeAliasRow,
    MemeAssetRow,
    MemeTemplateRow,
    MemeVersionBlobRow,
    MemeVersionRow,
)
from al1s.adapters.postgres.models import BlobObjectRow
from al1s.meme_workshop.builtins import BUILTINS, RESERVED_WORDS
from al1s.meme_workshop.contracts import TemplateWrite
from al1s.meme_workshop.records import TemplateRecord


class MemeCatalogRepository(MemeRepositoryBase):
    def ensure_builtins(self) -> None:
        for identity, data in BUILTINS:
            with self.sessions() as session:
                existing = session.get(MemeTemplateRow, identity)
            if existing is None:
                self.save(identity, data, expected_version=0, operation_id="builtin-seed-v1")
                with self.sessions.begin() as session:
                    row = session.get(MemeTemplateRow, identity)
                    if row:
                        row.builtin = True

    def list_templates(
        self, *, after_id: UUID | None = None, limit: int = 50, enabled_only: bool = False
    ) -> list[TemplateRecord]:
        if not 1 <= limit <= 501:
            raise ValueError("invalid_page_limit")
        with self.sessions() as session:
            query = (
                select(MemeTemplateRow, MemeVersionRow)
                .join(
                    MemeVersionRow,
                    MemeTemplateRow.current_version_id == MemeVersionRow.id,
                )
                .where(MemeTemplateRow.deleted_at.is_(None))
                .order_by(MemeTemplateRow.id)
                .limit(limit)
            )
            if after_id:
                query = query.where(MemeTemplateRow.id > after_id)
            if enabled_only:
                query = query.where(MemeTemplateRow.enabled)
            return self.records(
                session, [(row, version) for row, version in session.execute(query)]
            )

    def get(self, identity: UUID) -> TemplateRecord | None:
        with self.sessions() as session:
            pair = session.execute(
                select(MemeTemplateRow, MemeVersionRow)
                .join(
                    MemeVersionRow,
                    MemeTemplateRow.current_version_id == MemeVersionRow.id,
                )
                .where(MemeTemplateRow.id == identity, MemeTemplateRow.deleted_at.is_(None))
            ).first()
            return None if pair is None else self.records(session, [(pair[0], pair[1])])[0]

    def find_keyword(self, keyword: str) -> TemplateRecord | None:
        with self.sessions() as session:
            pair = session.execute(
                select(MemeTemplateRow, MemeVersionRow)
                .join(
                    MemeAliasRow,
                    MemeAliasRow.template_id == MemeTemplateRow.id,
                )
                .join(MemeVersionRow, MemeTemplateRow.current_version_id == MemeVersionRow.id)
                .where(
                    MemeAliasRow.normalized_alias == keyword,
                    MemeTemplateRow.enabled,
                    MemeTemplateRow.deleted_at.is_(None),
                )
            ).first()
            return None if pair is None else self.records(session, [(pair[0], pair[1])])[0]

    def random_candidates(self, avatars: int, images: int, texts: int) -> list[TemplateRecord]:
        with self.sessions() as session:
            query = (
                select(MemeTemplateRow, MemeVersionRow)
                .join(
                    MemeVersionRow,
                    MemeTemplateRow.current_version_id == MemeVersionRow.id,
                )
                .where(
                    MemeTemplateRow.deleted_at.is_(None),
                    MemeTemplateRow.enabled,
                    MemeTemplateRow.random_enabled,
                    MemeVersionRow.min_avatar_count <= avatars,
                    MemeVersionRow.min_image_count <= images,
                    MemeVersionRow.min_text_count <= texts,
                )
                .order_by(MemeTemplateRow.id)
                .limit(500)
            )
            return self.records(
                session, [(row, version) for row, version in session.execute(query)]
            )

    def save(
        self, identity: UUID, data: TemplateWrite, *, expected_version: int, operation_id: str
    ) -> TemplateRecord:
        key = self.mutation_key("template", identity, operation_id)
        digest = fingerprint({"data": data.model_dump(mode="json"), "expected": expected_version})
        try:
            with self.sessions.begin() as session:
                self.lock(session)
                previous = self.replay(session, key, digest)
                if previous is not None:
                    return TemplateRecord.model_validate(previous)
                row = self._save_row(session, identity, data, expected_version)
                version = self._save_version(session, row, data)
                self._save_aliases(session, row, data)
                row.current_version_id = version.id
                session.flush()
                result = self.records(session, [(row, version)])[0]
                self.add_mutation(
                    session,
                    key,
                    digest,
                    result.model_dump(mode="json"),
                    template_id=identity,
                    version_id=version.id,
                )
                return result
        except IntegrityError as exc:
            raise ValueError("keyword_or_resource_conflict") from exc

    @staticmethod
    def _save_row(
        session: Session, identity: UUID, data: TemplateWrite, expected: int
    ) -> MemeTemplateRow:
        now = datetime.now(UTC)
        row = session.get(MemeTemplateRow, identity, with_for_update=True)
        if row is None:
            if expected != 0:
                raise ValueError("template_conflict")
            count = (
                session.scalar(
                    select(func.count())
                    .select_from(MemeTemplateRow)
                    .where(
                        MemeTemplateRow.deleted_at.is_(None),
                    )
                )
                or 0
            )
            if count >= 500:
                raise ValueError("template_limit")
            row = MemeTemplateRow(id=identity, created_at=now, builtin=False, row_version=1)
            session.add(row)
        elif row.deleted_at is not None or row.row_version != expected:
            raise ValueError("template_conflict")
        else:
            row.row_version += 1
        row.name, row.engine_kind = data.name, data.layout.engine
        row.enabled, row.random_enabled = data.enabled, data.random_enabled
        row.updated_at = now
        session.flush()
        return row

    @staticmethod
    def _save_version(
        session: Session, row: MemeTemplateRow, data: TemplateWrite
    ) -> MemeVersionRow:
        avatar_count, image_count, text_count = data.layout.requirements()
        version = MemeVersionRow(
            id=uuid4(),
            template_id=row.id,
            version_no=row.row_version,
            schema_version=1,
            min_avatar_count=avatar_count,
            min_image_count=image_count,
            min_text_count=text_count,
            layout=data.layout.model_dump(mode="json"),
            created_at=datetime.now(UTC),
        )
        session.add(version)
        session.flush()
        references = {"background": data.background_blob_id, "foreground": data.foreground_blob_id}
        ids = [identity for identity in references.values() if identity is not None]
        if ids:
            ready = set(
                session.scalars(
                    select(BlobObjectRow.id)
                    .join(MemeAssetRow, MemeAssetRow.blob_id == BlobObjectRow.id)
                    .where(
                        BlobObjectRow.id.in_(ids),
                        BlobObjectRow.status == "ready",
                    )
                    .order_by(BlobObjectRow.id)
                    .with_for_update(read=True, of=[BlobObjectRow, MemeAssetRow])
                ).all()
            )
            if ready != set(ids):
                raise ValueError("asset_not_ready")
        session.add_all(
            MemeVersionBlobRow(version_id=version.id, role=role, blob_id=identity)
            for role, identity in references.items()
            if identity is not None
        )
        return version

    @staticmethod
    def _save_aliases(session: Session, row: MemeTemplateRow, data: TemplateWrite) -> None:
        words = [data.keyword, *data.aliases]
        if RESERVED_WORDS.intersection(words):
            raise ValueError("keyword_reserved")
        occupied = session.scalar(
            select(MemeAliasRow.normalized_alias)
            .where(
                MemeAliasRow.normalized_alias.in_(words),
                MemeAliasRow.template_id != row.id,
            )
            .limit(1)
        )
        if occupied:
            raise ValueError("keyword_conflict")
        session.execute(delete(MemeAliasRow).where(MemeAliasRow.template_id == row.id))
        session.add_all(
            MemeAliasRow(normalized_alias=word, template_id=row.id, is_primary=index == 0)
            for index, word in enumerate(words)
        )

    def delete(self, identity: UUID, *, expected_version: int, operation_id: str) -> None:
        key = self.mutation_key("delete", identity, operation_id)
        digest = fingerprint({"expected": expected_version})
        with self.sessions.begin() as session:
            self.lock(session)
            if self.replay(session, key, digest) is not None:
                return
            row = session.get(MemeTemplateRow, identity, with_for_update=True)
            if row is None or row.deleted_at or row.row_version != expected_version:
                raise ValueError("template_conflict")
            if row.builtin:
                raise ValueError("builtin_disable_only")
            row.deleted_at, row.enabled = datetime.now(UTC), False
            row.current_version_id = None
            row.row_version += 1
            session.execute(delete(MemeAliasRow).where(MemeAliasRow.template_id == identity))
            self.add_mutation(session, key, digest, {"deleted": True}, template_id=identity)
