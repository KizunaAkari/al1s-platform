"""Persist and fence manual GC requests without exposing object operations to HTTP."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.models import StorageGcRequestRow
from al1s.kernel.storage_gc import ClaimedStorageGcRequest, StorageGcRequest
from al1s.kernel.types import GcBatchResult


def _record(row: StorageGcRequestRow) -> StorageGcRequest:
    return StorageGcRequest(
        id=row.id, status=row.status, requested_at=row.requested_at,
        completed_at=row.completed_at, attempt_count=row.attempt_count,
        claimed=row.claimed, deleted=row.deleted, failed=row.failed, stale=row.stale,
        error_type=row.error_type,
    )


class PostgresStorageGcRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def latest(self) -> StorageGcRequest | None:
        with self._sessions() as session:
            row = session.scalars(
                select(StorageGcRequestRow)
                .order_by(StorageGcRequestRow.requested_at.desc(), StorageGcRequestRow.id.desc())
                .limit(1)
            ).first()
            return _record(row) if row else None

    def submit(self) -> StorageGcRequest:
        now, request_id = datetime.now(UTC), uuid4()
        with self._sessions.begin() as session:
            inserted = session.scalar(
                insert(StorageGcRequestRow)
                .values(id=request_id, status="pending", requested_at=now)
                .on_conflict_do_nothing()
                .returning(StorageGcRequestRow.id)
            )
            if inserted is not None:
                row = session.get(StorageGcRequestRow, request_id)
            else:
                row = session.scalars(
                    select(StorageGcRequestRow)
                    .where(StorageGcRequestRow.status.in_(("pending", "processing")))
                    .limit(1)
                ).first()
            if row is None:
                raise RuntimeError("storage cleanup request changed concurrently; retry")
            return _record(row)

    def claim(self, now: datetime) -> ClaimedStorageGcRequest | None:
        with self._sessions.begin() as session:
            row = session.scalars(
                select(StorageGcRequestRow)
                .where(or_(
                    StorageGcRequestRow.status == "pending",
                    (StorageGcRequestRow.status == "processing")
                    & (StorageGcRequestRow.locked_until <= now),
                ))
                .order_by(StorageGcRequestRow.requested_at, StorageGcRequestRow.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if row is None:
                return None
            if row.attempt_count >= 3:
                row.status = "failed"
                row.completed_at = now
                row.locked_until = None
                row.error_type = "WorkerLeaseExpired"
                return None
            row.status = "processing"
            row.attempt_count += 1
            row.locked_until = now + timedelta(minutes=30)
            return ClaimedStorageGcRequest(row.id, row.attempt_count)

    def settle(
        self, claim: ClaimedStorageGcRequest, now: datetime,
        result: GcBatchResult | None, error_type: str | None = None,
    ) -> bool:
        values: dict[str, object] = {
            "status": "failed" if error_type else "completed",
            "completed_at": now,
            "locked_until": None,
            "error_type": error_type[:100] if error_type else None,
        }
        if result is not None:
            values.update(
                claimed=result.claimed, deleted=result.deleted,
                failed=result.failed, stale=result.stale,
            )
        with self._sessions.begin() as session:
            updated = session.scalar(
                update(StorageGcRequestRow)
                .where(
                    StorageGcRequestRow.id == claim.id,
                    StorageGcRequestRow.status == "processing",
                    StorageGcRequestRow.attempt_count == claim.attempt_count,
                    StorageGcRequestRow.locked_until > now,
                )
                .values(**values)
                .returning(StorageGcRequestRow.id)
            )
            return updated is not None
