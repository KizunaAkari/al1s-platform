from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, exists, func, literal, or_, select, tuple_, update, values
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import column

from al1s.adapters.postgres.blob_references import no_blob_references
from al1s.adapters.postgres.models import (
    AuditLogRow,
    BlobObjectRow,
    GcJobRow,
    InboxReceiptRow,
    OutboxEventRow,
)
from al1s.kernel.types import (
    BlobRecord,
    BlobStatus,
    ClaimedGcJob,
    ClaimedOutboxEvent,
    FailedDelivery,
    FailedGcJob,
    GcJobStatus,
    KernelMaintenanceSnapshot,
    NewAuditEntry,
    NewBlob,
    NewOutboxEvent,
    OutboxStatus,
    PreparedGcJob,
)

MAX_WORKER_BATCH = 50


def _validate_batch_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_WORKER_BATCH:
        raise ValueError(f"limit must be between 1 and {MAX_WORKER_BATCH}")
    return limit


def _retry_delay(
    attempt_count: int,
    base_retry_delay: timedelta,
    max_retry_delay: timedelta,
) -> timedelta:
    multiplier = 2 ** max(0, attempt_count - 1)
    delay_seconds = min(
        base_retry_delay.total_seconds() * multiplier,
        max_retry_delay.total_seconds(),
    )
    return timedelta(seconds=delay_seconds)


def _outbox_projection() -> tuple[Any, ...]:
    return (
        OutboxEventRow.id,
        OutboxEventRow.event_type,
        OutboxEventRow.schema_version,
        OutboxEventRow.aggregate_type,
        OutboxEventRow.aggregate_id,
        OutboxEventRow.correlation_id,
        OutboxEventRow.occurred_at,
        OutboxEventRow.payload,
        OutboxEventRow.attempt_count,
        OutboxEventRow.row_version,
    )


class PostgresOutboxRepository:
    def __init__(self, session: Session, *, owner_module: str | None = None) -> None:
        self._session = session
        if owner_module not in (None, "platform", "maa", "information"):
            raise ValueError("unknown module owner")
        # None is reserved for the cross-module infrastructure dispatcher.
        self._owner_module = owner_module

    def _scope(self) -> Any:
        if self._owner_module is None:
            return literal(True)
        return OutboxEventRow.owner_module == self._owner_module

    def add(self, event: NewOutboxEvent) -> None:
        self._session.add(
            OutboxEventRow(
                owner_module=self._owner_module or "unassigned",
                id=event.event_id,
                event_type=event.event_type,
                schema_version=event.schema_version,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                correlation_id=event.correlation_id,
                occurred_at=event.occurred_at,
                payload=dict(event.payload),
                status=OutboxStatus.PENDING.value,
                attempt_count=0,
                available_at=event.occurred_at,
                row_version=1,
            )
        )

    def claim_batch(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
    ) -> list[ClaimedOutboxEvent]:
        bounded_limit = _validate_batch_limit(limit)
        claimable_ids = (
            select(OutboxEventRow.id)
            .where(
                self._scope(),
                or_(
                    and_(
                        OutboxEventRow.status == OutboxStatus.PENDING.value,
                        OutboxEventRow.available_at <= now,
                    ),
                    and_(
                        OutboxEventRow.status == OutboxStatus.PROCESSING.value,
                        OutboxEventRow.locked_until <= now,
                    ),
                )
            )
            .order_by(OutboxEventRow.available_at, OutboxEventRow.created_at)
            .limit(bounded_limit)
            .with_for_update(skip_locked=True)
            .cte("claimable_outbox")
        )
        statement = (
            update(OutboxEventRow)
            .where(OutboxEventRow.id.in_(select(claimable_ids.c.id)))
            .values(
                status=OutboxStatus.PROCESSING.value,
                locked_by=worker_id,
                locked_until=now + lease_duration,
                row_version=OutboxEventRow.row_version + 1,
            )
            .returning(*_outbox_projection())
        )
        rows = self._session.execute(statement).all()
        return [
            ClaimedOutboxEvent(
                event_id=row.id,
                event_type=row.event_type,
                schema_version=row.schema_version,
                aggregate_type=row.aggregate_type,
                aggregate_id=row.aggregate_id,
                correlation_id=row.correlation_id,
                occurred_at=row.occurred_at,
                payload=dict(row.payload),
                attempt_count=row.attempt_count,
                row_version=row.row_version,
            )
            for row in rows
        ]

    def mark_published(
        self,
        events: Sequence[ClaimedOutboxEvent],
        *,
        worker_id: str,
        published_at: datetime,
    ) -> int:
        if not events:
            return 0
        expected = [(event.event_id, event.row_version) for event in events]
        statement = (
            update(OutboxEventRow)
            .where(
                tuple_(OutboxEventRow.id, OutboxEventRow.row_version).in_(expected),
                self._scope(),
                OutboxEventRow.status == OutboxStatus.PROCESSING.value,
                OutboxEventRow.locked_by == worker_id,
            )
            .values(
                status=OutboxStatus.PUBLISHED.value,
                published_at=published_at,
                locked_by=None,
                locked_until=None,
                last_error_type=None,
                row_version=OutboxEventRow.row_version + 1,
            )
            .returning(OutboxEventRow.id)
        )
        return len(self._session.execute(statement).scalars().all())

    def mark_failed(
        self,
        deliveries: Sequence[FailedDelivery],
        *,
        worker_id: str,
        failed_at: datetime,
        max_attempts: int,
        base_retry_delay: timedelta,
        max_retry_delay: timedelta,
    ) -> int:
        if not deliveries:
            return 0
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        update_values = values(
            column("event_id", OutboxEventRow.id.type),
            column("expected_version", OutboxEventRow.row_version.type),
            column("next_attempt_count", OutboxEventRow.attempt_count.type),
            column("next_status", OutboxEventRow.status.type),
            column("next_available_at", OutboxEventRow.available_at.type),
            column("error_type", OutboxEventRow.last_error_type.type),
            name="failed_outbox",
        ).data(
            [
                (
                    delivery.event_id,
                    delivery.row_version,
                    delivery.attempt_count + 1,
                    (
                        OutboxStatus.DEAD_LETTER.value
                        if delivery.attempt_count + 1 >= max_attempts
                        else OutboxStatus.PENDING.value
                    ),
                    failed_at
                    + _retry_delay(
                        delivery.attempt_count + 1,
                        base_retry_delay,
                        max_retry_delay,
                    ),
                    delivery.error_type,
                )
                for delivery in deliveries
            ]
        )
        statement = (
            update(OutboxEventRow)
            .where(
                OutboxEventRow.id == update_values.c.event_id,
                self._scope(),
                OutboxEventRow.row_version == update_values.c.expected_version,
                OutboxEventRow.status == OutboxStatus.PROCESSING.value,
                OutboxEventRow.locked_by == worker_id,
            )
            .values(
                status=update_values.c.next_status,
                attempt_count=update_values.c.next_attempt_count,
                available_at=update_values.c.next_available_at,
                locked_by=None,
                locked_until=None,
                last_error_type=update_values.c.error_type,
                row_version=OutboxEventRow.row_version + 1,
            )
            .returning(OutboxEventRow.id)
        )
        return len(self._session.execute(statement).scalars().all())


class PostgresInboxRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record_once(self, consumer_name: str, event_id: UUID, completed_at: datetime) -> bool:
        statement = (
            insert(InboxReceiptRow)
            .values(
                consumer_name=consumer_name,
                event_id=event_id,
                received_at=completed_at,
                completed_at=completed_at,
            )
            .on_conflict_do_nothing(
                index_elements=[InboxReceiptRow.consumer_name, InboxReceiptRow.event_id]
            )
            .returning(InboxReceiptRow.event_id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None


class PostgresBlobCatalogRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_pending(self, blob: NewBlob) -> None:
        self._session.add(
            BlobObjectRow(
                id=blob.blob_id,
                sha256=blob.sha256,
                size_bytes=blob.size_bytes,
                media_type=blob.media_type,
                object_key=blob.object_key,
                status=BlobStatus.PENDING.value,
                row_version=1,
            )
        )

    def find_ready_by_id(self, blob_id: UUID) -> BlobRecord | None:
        statement: Select[tuple[BlobObjectRow]] = select(BlobObjectRow).where(
            BlobObjectRow.id == blob_id,
            BlobObjectRow.status == BlobStatus.READY.value,
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _to_blob_record(row)

    def find_ready_by_ids(self, blob_ids: Sequence[UUID]) -> dict[UUID, BlobRecord]:
        if not blob_ids:
            return {}
        statement = select(BlobObjectRow).where(
            BlobObjectRow.id.in_(set(blob_ids)), BlobObjectRow.status == BlobStatus.READY.value,
        )
        return {row.id: _to_blob_record(row) for row in self._session.execute(statement).scalars()}

    def find_ready_by_sha256(self, sha256: str) -> BlobRecord | None:
        statement: Select[tuple[BlobObjectRow]] = (
            select(BlobObjectRow)
            .where(
                BlobObjectRow.sha256 == sha256,
                BlobObjectRow.status == BlobStatus.READY.value,
            )
            .limit(1)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _to_blob_record(row)

    def find_ready_by_sha256_many(self, sha256s: Sequence[str]) -> dict[str, BlobRecord]:
        if not sha256s:
            return {}
        statement = select(BlobObjectRow).where(
            BlobObjectRow.sha256.in_(set(sha256s)),
            BlobObjectRow.status == BlobStatus.READY.value,
        )
        return {
            row.sha256: _to_blob_record(row)
            for row in self._session.execute(statement).scalars().all()
        }

    def mark_ready(self, blob_id: UUID, expected_version: int, ready_at: datetime) -> bool:
        statement = (
            update(BlobObjectRow)
            .where(
                BlobObjectRow.id == blob_id,
                BlobObjectRow.row_version == expected_version,
                BlobObjectRow.status == BlobStatus.PENDING.value,
            )
            .values(
                status=BlobStatus.READY.value,
                ready_at=ready_at,
                row_version=BlobObjectRow.row_version + 1,
            )
            .returning(BlobObjectRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def mark_ready_many(
        self,
        expected: Sequence[tuple[UUID, int]],
        ready_at: datetime,
    ) -> int:
        if not expected:
            return 0
        statement = (
            update(BlobObjectRow)
            .where(
                tuple_(BlobObjectRow.id, BlobObjectRow.row_version).in_(expected),
                BlobObjectRow.status == BlobStatus.PENDING.value,
            )
            .values(
                status=BlobStatus.READY.value,
                ready_at=ready_at,
                row_version=BlobObjectRow.row_version + 1,
            )
            .returning(BlobObjectRow.id)
        )
        return len(self._session.execute(statement).scalars().all())

    def mark_quarantined(self, blob_id: UUID, expected_version: int) -> bool:
        statement = (
            update(BlobObjectRow)
            .where(
                BlobObjectRow.id == blob_id,
                BlobObjectRow.row_version == expected_version,
                BlobObjectRow.status == BlobStatus.PENDING.value,
            )
            .values(
                status=BlobStatus.QUARANTINED.value,
                row_version=BlobObjectRow.row_version + 1,
            )
            .returning(BlobObjectRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def mark_quarantined_many(self, expected: Sequence[tuple[UUID, int]]) -> int:
        if not expected:
            return 0
        statement = (
            update(BlobObjectRow)
            .where(
                tuple_(BlobObjectRow.id, BlobObjectRow.row_version).in_(expected),
                BlobObjectRow.status == BlobStatus.PENDING.value,
            )
            .values(
                status=BlobStatus.QUARANTINED.value,
                row_version=BlobObjectRow.row_version + 1,
            )
            .returning(BlobObjectRow.id)
        )
        return len(self._session.execute(statement).scalars().all())

    def prepare_deletions(self, jobs: Sequence[ClaimedGcJob]) -> list[PreparedGcJob]:
        if not jobs:
            return []
        jobs_by_blob = {job.blob_id: job for job in jobs}
        expected = [(job.blob_id, job.blob_row_version) for job in jobs]
        # Fence new references before the fresh reference query (0024 triggers).
        locked = self._session.scalars(
            select(BlobObjectRow.id).where(BlobObjectRow.id.in_(jobs_by_blob))
            .order_by(BlobObjectRow.id).with_for_update(skip_locked=True)
        ).all()
        statement = (
            update(BlobObjectRow)
            .where(
                tuple_(BlobObjectRow.id, BlobObjectRow.row_version).in_(expected),
                BlobObjectRow.id.in_(locked),
                *no_blob_references(),
                BlobObjectRow.status.in_(
                    [
                        BlobStatus.PENDING.value,
                        BlobStatus.READY.value,
                        BlobStatus.QUARANTINED.value,
                        BlobStatus.DELETING.value,
                    ]
                ),
            )
            .values(
                status=BlobStatus.DELETING.value,
                row_version=BlobObjectRow.row_version + 1,
            )
            .returning(BlobObjectRow.id, BlobObjectRow.object_key, BlobObjectRow.row_version)
        )
        return [
            PreparedGcJob(
                job_id=jobs_by_blob[row.id].job_id,
                blob_id=row.id,
                object_key=row.object_key,
                blob_row_version=row.row_version,
                attempt_count=jobs_by_blob[row.id].attempt_count,
                job_row_version=jobs_by_blob[row.id].row_version,
            )
            for row in self._session.execute(statement).all()
        ]

    def mark_deleted(
        self,
        jobs: Sequence[PreparedGcJob],
        deleted_at: datetime,
    ) -> int:
        if not jobs:
            return 0
        expected = [(job.blob_id, job.blob_row_version) for job in jobs]
        statement = (
            update(BlobObjectRow)
            .where(
                tuple_(BlobObjectRow.id, BlobObjectRow.row_version).in_(expected),
                BlobObjectRow.status == BlobStatus.DELETING.value,
            )
            .values(
                status=BlobStatus.DELETED.value,
                deleted_at=deleted_at,
                row_version=BlobObjectRow.row_version + 1,
            )
            .returning(BlobObjectRow.id)
        )
        return len(self._session.execute(statement).scalars().all())


class PostgresGcJobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def schedule(self, job_id: UUID, blob_id: UUID, available_at: datetime) -> bool:
        eligible_blob = exists(
            select(BlobObjectRow.id).where(
                BlobObjectRow.id == blob_id,
                BlobObjectRow.status.in_(
                    [
                        BlobStatus.PENDING.value,
                        BlobStatus.READY.value,
                        BlobStatus.QUARANTINED.value,
                        BlobStatus.DELETING.value,
                    ]
                ),
            )
        )
        statement = (
            insert(GcJobRow)
            .from_select(
                [
                    GcJobRow.id,
                    GcJobRow.blob_id,
                    GcJobRow.status,
                    GcJobRow.attempt_count,
                    GcJobRow.available_at,
                    GcJobRow.row_version,
                ],
                select(
                    literal(job_id),
                    literal(blob_id),
                    literal(GcJobStatus.PENDING.value),
                    literal(0),
                    literal(available_at),
                    literal(1),
                ).where(eligible_blob),
            )
            .on_conflict_do_update(
                index_elements=[GcJobRow.blob_id],
                index_where=GcJobRow.status.in_(
                    [GcJobStatus.PENDING.value, GcJobStatus.PROCESSING.value]
                ),
                set_={"available_at": available_at, "row_version": GcJobRow.row_version + 1},
                where=and_(GcJobRow.status == GcJobStatus.PENDING.value,
                           GcJobRow.available_at > available_at),
            )
            .returning(GcJobRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def schedule_many(
        self,
        jobs: Sequence[tuple[UUID, UUID]],
        available_at: datetime,
    ) -> int:
        if not jobs:
            return 0
        # A single INSERT/UPDATE cannot target the same existing row twice.
        jobs = list({blob_id: (job_id, blob_id) for job_id, blob_id in jobs}.values())
        job_values = values(
            column("job_id", GcJobRow.id.type),
            column("blob_id", GcJobRow.blob_id.type),
            name="gc_jobs_to_schedule",
        ).data(jobs)
        statement = (
            insert(GcJobRow)
            .from_select(
                [
                    GcJobRow.id,
                    GcJobRow.blob_id,
                    GcJobRow.status,
                    GcJobRow.attempt_count,
                    GcJobRow.available_at,
                    GcJobRow.row_version,
                ],
                select(
                    job_values.c.job_id,
                    job_values.c.blob_id,
                    literal(GcJobStatus.PENDING.value),
                    literal(0),
                    literal(available_at),
                    literal(1),
                )
                .join(BlobObjectRow, BlobObjectRow.id == job_values.c.blob_id)
                .where(
                    BlobObjectRow.status.in_(
                        [
                            BlobStatus.PENDING.value,
                            BlobStatus.READY.value,
                            BlobStatus.QUARANTINED.value,
                            BlobStatus.DELETING.value,
                        ]
                    )
                ),
            )
            .on_conflict_do_update(
                index_elements=[GcJobRow.blob_id],
                index_where=GcJobRow.status.in_(
                    [GcJobStatus.PENDING.value, GcJobStatus.PROCESSING.value]
                ),
                set_={"available_at": available_at, "row_version": GcJobRow.row_version + 1},
                where=and_(GcJobRow.status == GcJobStatus.PENDING.value,
                           GcJobRow.available_at > available_at),
            )
            .returning(GcJobRow.id)
        )
        return len(self._session.execute(statement).scalars().all())

    def claim_batch(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
    ) -> list[ClaimedGcJob]:
        bounded_limit = _validate_batch_limit(limit)
        claimable_ids = (
            select(GcJobRow.id)
            .where(
                exists(select(BlobObjectRow.id).where(
                    BlobObjectRow.id == GcJobRow.blob_id,
                    *no_blob_references(),
                )),
                or_(
                    and_(
                        GcJobRow.status == GcJobStatus.PENDING.value,
                        GcJobRow.available_at <= now,
                    ),
                    and_(
                        GcJobRow.status == GcJobStatus.PROCESSING.value,
                        GcJobRow.locked_until <= now,
                    ),
                )
            )
            .order_by(GcJobRow.available_at, GcJobRow.created_at)
            .limit(bounded_limit)
            .with_for_update(skip_locked=True)
            .cte("claimable_gc_jobs")
        )
        statement = (
            update(GcJobRow)
            .where(GcJobRow.id.in_(select(claimable_ids.c.id)))
            .values(
                status=GcJobStatus.PROCESSING.value,
                locked_by=worker_id,
                locked_until=now + lease_duration,
                row_version=GcJobRow.row_version + 1,
            )
            .returning(
                GcJobRow.id,
                GcJobRow.blob_id,
                GcJobRow.attempt_count,
                GcJobRow.row_version,
            )
        )
        claimed = self._session.execute(statement).all()
        if not claimed:
            return []
        claimed_by_id = {row.id: row for row in claimed}
        details = self._session.execute(
            select(
                GcJobRow.id,
                GcJobRow.blob_id,
                BlobObjectRow.object_key,
                BlobObjectRow.status,
                BlobObjectRow.row_version.label("blob_row_version"),
            )
            .join(BlobObjectRow, BlobObjectRow.id == GcJobRow.blob_id)
            .where(GcJobRow.id.in_(claimed_by_id))
        ).all()
        return [
            ClaimedGcJob(
                job_id=row.id,
                blob_id=row.blob_id,
                object_key=row.object_key,
                blob_status=BlobStatus(row.status),
                blob_row_version=row.blob_row_version,
                attempt_count=claimed_by_id[row.id].attempt_count,
                row_version=claimed_by_id[row.id].row_version,
            )
            for row in details
        ]

    def mark_completed(
        self,
        jobs: Sequence[ClaimedGcJob],
        *,
        worker_id: str,
        completed_at: datetime,
    ) -> int:
        if not jobs:
            return 0
        expected = [(job.job_id, job.row_version) for job in jobs]
        statement = (
            update(GcJobRow)
            .where(
                tuple_(GcJobRow.id, GcJobRow.row_version).in_(expected),
                GcJobRow.status == GcJobStatus.PROCESSING.value,
                GcJobRow.locked_by == worker_id,
            )
            .values(
                status=GcJobStatus.COMPLETED.value,
                completed_at=completed_at,
                locked_by=None,
                locked_until=None,
                last_error_type=None,
                row_version=GcJobRow.row_version + 1,
            )
            .returning(GcJobRow.id)
        )
        return len(self._session.execute(statement).scalars().all())

    def mark_failed(
        self,
        jobs: Sequence[FailedGcJob],
        *,
        worker_id: str,
        failed_at: datetime,
        max_attempts: int,
        base_retry_delay: timedelta,
        max_retry_delay: timedelta,
    ) -> int:
        if not jobs:
            return 0
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        update_values = values(
            column("job_id", GcJobRow.id.type),
            column("expected_version", GcJobRow.row_version.type),
            column("next_attempt_count", GcJobRow.attempt_count.type),
            column("next_status", GcJobRow.status.type),
            column("next_available_at", GcJobRow.available_at.type),
            column("error_type", GcJobRow.last_error_type.type),
            name="failed_gc_jobs",
        ).data(
            [
                (
                    job.job_id,
                    job.row_version,
                    job.attempt_count + 1,
                    (
                        GcJobStatus.DEAD_LETTER.value
                        if job.attempt_count + 1 >= max_attempts
                        else GcJobStatus.PENDING.value
                    ),
                    failed_at
                    + _retry_delay(
                        job.attempt_count + 1,
                        base_retry_delay,
                        max_retry_delay,
                    ),
                    job.error_type,
                )
                for job in jobs
            ]
        )
        statement = (
            update(GcJobRow)
            .where(
                GcJobRow.id == update_values.c.job_id,
                GcJobRow.row_version == update_values.c.expected_version,
                GcJobRow.status == GcJobStatus.PROCESSING.value,
                GcJobRow.locked_by == worker_id,
            )
            .values(
                status=update_values.c.next_status,
                attempt_count=update_values.c.next_attempt_count,
                available_at=update_values.c.next_available_at,
                locked_by=None,
                locked_until=None,
                last_error_type=update_values.c.error_type,
                row_version=GcJobRow.row_version + 1,
            )
            .returning(GcJobRow.id)
        )
        return len(self._session.execute(statement).scalars().all())


class PostgresAuditRepository:
    def __init__(self, session: Session, *, owner_module: str = "unassigned") -> None:
        self._session = session
        if owner_module not in ("unassigned", "platform", "maa", "information"):
            raise ValueError("unknown module owner")
        self._owner_module = owner_module

    def add(self, entry: NewAuditEntry) -> None:
        _reject_sensitive_audit_keys(entry.details)
        self._session.add(
            AuditLogRow(
                owner_module=self._owner_module,
                id=entry.audit_id,
                actor_type=entry.actor_type,
                actor_id=entry.actor_id,
                action=entry.action,
                target_type=entry.target_type,
                target_id=entry.target_id,
                correlation_id=entry.correlation_id,
                details=dict(entry.details),
                summary=entry.summary,
            )
        )


class PostgresKernelMaintenanceReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def snapshot(self) -> KernelMaintenanceSnapshot:
        with self._session_factory() as session:
            outbox = session.execute(
                select(
                    func.count().filter(OutboxEventRow.status == OutboxStatus.PENDING.value),
                    func.count().filter(OutboxEventRow.status == OutboxStatus.PROCESSING.value),
                    func.count().filter(OutboxEventRow.status == OutboxStatus.DEAD_LETTER.value),
                    func.min(OutboxEventRow.created_at).filter(
                        OutboxEventRow.status == OutboxStatus.PENDING.value
                    ),
                )
            ).one()
            gc_jobs = session.execute(
                select(
                    func.count().filter(GcJobRow.status == GcJobStatus.PENDING.value),
                    func.count().filter(GcJobRow.status == GcJobStatus.PROCESSING.value),
                    func.count().filter(GcJobRow.status == GcJobStatus.DEAD_LETTER.value),
                )
            ).one()
            blobs = session.execute(
                select(
                    func.count().filter(BlobObjectRow.status == BlobStatus.PENDING.value),
                    func.count().filter(BlobObjectRow.status == BlobStatus.QUARANTINED.value),
                )
            ).one()
            now = datetime.now(UTC)
            reclaimable = session.execute(
                select(func.count(), func.coalesce(func.sum(BlobObjectRow.size_bytes), 0))
                .select_from(GcJobRow)
                .join(BlobObjectRow, BlobObjectRow.id == GcJobRow.blob_id)
                .where(
                    BlobObjectRow.status != BlobStatus.DELETED.value,
                    *no_blob_references(),
                    or_(
                        and_(GcJobRow.status == GcJobStatus.PENDING.value,
                             GcJobRow.available_at <= now),
                        and_(GcJobRow.status == GcJobStatus.PROCESSING.value,
                             GcJobRow.locked_until <= now),
                    ),
                )
            ).one()
        return KernelMaintenanceSnapshot(
            pending_outbox=outbox[0],
            processing_outbox=outbox[1],
            dead_letter_outbox=outbox[2],
            oldest_pending_outbox_at=outbox[3],
            pending_gc_jobs=gc_jobs[0],
            processing_gc_jobs=gc_jobs[1],
            dead_letter_gc_jobs=gc_jobs[2],
            pending_blobs=blobs[0],
            quarantined_blobs=blobs[1],
            reclaimable_blobs=reclaimable[0],
            reclaimable_bytes=reclaimable[1],
        )


def _to_blob_record(row: BlobObjectRow) -> BlobRecord:
    return BlobRecord(
        blob_id=row.id,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        media_type=row.media_type,
        object_key=row.object_key,
        status=BlobStatus(row.status),
        row_version=row.row_version,
        created_at=row.created_at,
        ready_at=row.ready_at,
        deleted_at=row.deleted_at,
    )


_SENSITIVE_AUDIT_KEY_PARTS = (
    "authorization",
    "cookie",
    "password",
    "private_key",
    "secret",
    "token",
)


def _reject_sensitive_audit_keys(value: object, path: str = "details") -> None:
    if isinstance(value, dict):
        for key, nested_value in value.items():
            normalized_key = str(key).casefold().replace("-", "_")
            if any(part in normalized_key for part in _SENSITIVE_AUDIT_KEY_PARTS):
                raise ValueError(f"sensitive audit field is not allowed: {path}.{key}")
            _reject_sensitive_audit_keys(nested_value, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested_value in enumerate(value):
            _reject_sensitive_audit_keys(nested_value, f"{path}[{index}]")
