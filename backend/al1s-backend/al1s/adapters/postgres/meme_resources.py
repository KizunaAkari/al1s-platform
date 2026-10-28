from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert

from al1s.adapters.postgres.meme_base import MemeRepositoryBase
from al1s.adapters.postgres.meme_models import MemeAssetRow, MemeVersionBlobRow
from al1s.adapters.postgres.repositories import (
    PostgresBlobCatalogRepository,
    PostgresGcJobRepository,
)
from al1s.kernel.types import BlobRecord, NewBlob
from al1s.meme_workshop.records import AssetRecord


class MemeResourceRepository(MemeRepositoryBase):
    def reserve_asset(self, pending: NewBlob) -> BlobRecord | None:
        with self.sessions.begin() as session:
            catalog = PostgresBlobCatalogRepository(session)
            existing = catalog.find_ready_by_sha256(pending.sha256)
            if existing:
                return existing
            catalog.add_pending(pending)
            session.flush()
            PostgresGcJobRepository(session).schedule(
                uuid4(),
                pending.blob_id,
                datetime.now(UTC) + timedelta(hours=24),
            )
            return None

    def finalize_asset(self, pending: NewBlob, width: int, height: int) -> AssetRecord:
        with self.sessions.begin() as session:
            # Same finalize lock as Maa imports/uploads; READY hashes are shared globally.
            session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtext("al1s.maa.archive-import.finalize"),
                    )
                )
            )
            catalog = PostgresBlobCatalogRepository(session)
            blob = catalog.find_ready_by_sha256(pending.sha256)
            if blob is None:
                if not catalog.mark_ready(pending.blob_id, 1, datetime.now(UTC)):
                    raise ValueError("asset_reservation_expired")
                blob = catalog.find_ready_by_id(pending.blob_id)
            if blob is None:
                raise ValueError("asset_not_ready")
            session.execute(
                insert(MemeAssetRow)
                .values(
                    blob_id=blob.blob_id,
                    width=width,
                    height=height,
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                )
                .on_conflict_do_update(
                    index_elements=[MemeAssetRow.blob_id],
                    set_={
                        "expires_at": datetime.now(UTC) + timedelta(hours=24),
                    },
                )
            )
            PostgresGcJobRepository(session).schedule(
                uuid4(),
                blob.blob_id,
                datetime.now(UTC) + timedelta(hours=24),
            )
            return AssetRecord(blob_id=blob.blob_id, width=width, height=height)

    def asset_blobs(self, ids: list[UUID], *, lease: bool = True) -> dict[UUID, BlobRecord]:
        if len(ids) > 2:
            raise ValueError("asset_count_exceeded")
        with self.sessions.begin() as session:
            query = (
                select(MemeAssetRow.blob_id)
                .where(MemeAssetRow.blob_id.in_(ids))
                .order_by(
                    MemeAssetRow.blob_id,
                )
                .with_for_update()
            )
            owned = set(session.scalars(query))
            if owned != set(ids):
                raise ValueError("asset_not_found")
            if lease and ids:
                until = datetime.now(UTC) + timedelta(seconds=60)
                session.execute(
                    update(MemeAssetRow)
                    .where(MemeAssetRow.blob_id.in_(ids))
                    .values(
                        expires_at=func.greatest(MemeAssetRow.expires_at, until),
                    )
                )
            blobs = PostgresBlobCatalogRepository(session).find_ready_by_ids(ids)
            if set(blobs) != set(ids):
                raise ValueError("asset_not_ready")
            return blobs

    def cleanup_assets(self, now: datetime, *, limit: int = 50) -> None:
        with self.sessions.begin() as session:
            unused = (
                select(MemeAssetRow.blob_id)
                .where(
                    MemeAssetRow.expires_at <= now,
                    ~exists(
                        select(MemeVersionBlobRow.version_id).where(
                            MemeVersionBlobRow.blob_id == MemeAssetRow.blob_id,
                        )
                    ),
                )
                .order_by(MemeAssetRow.expires_at, MemeAssetRow.blob_id)
                .limit(limit)
                .with_for_update(
                    skip_locked=True,
                )
            )
            removed = list(
                session.scalars(
                    delete(MemeAssetRow)
                    .where(
                        MemeAssetRow.blob_id.in_(unused),
                    )
                    .returning(MemeAssetRow.blob_id)
                )
            )
            PostgresGcJobRepository(session).schedule_many(
                [(uuid4(), identity) for identity in removed],
                now,
            )
