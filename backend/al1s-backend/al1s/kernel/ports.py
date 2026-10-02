from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from types import TracebackType
from typing import BinaryIO, Protocol
from uuid import UUID

from al1s.kernel.types import (
    BlobObjectHead,
    BlobRecord,
    ClaimedGcJob,
    ClaimedOutboxEvent,
    FailedDelivery,
    FailedGcJob,
    KernelMaintenanceSnapshot,
    NewAuditEntry,
    NewBlob,
    NewOutboxEvent,
    PreparedGcJob,
)


class OutboxRepository(Protocol):
    def add(self, event: NewOutboxEvent) -> None: ...

    def claim_batch(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
    ) -> list[ClaimedOutboxEvent]: ...

    def mark_published(
        self,
        events: Sequence[ClaimedOutboxEvent],
        *,
        worker_id: str,
        published_at: datetime,
    ) -> int: ...

    def mark_failed(
        self,
        deliveries: Sequence[FailedDelivery],
        *,
        worker_id: str,
        failed_at: datetime,
        max_attempts: int,
        base_retry_delay: timedelta,
        max_retry_delay: timedelta,
    ) -> int: ...


class InboxRepository(Protocol):
    def record_once(self, consumer_name: str, event_id: UUID, completed_at: datetime) -> bool: ...


class BlobCatalogRepository(Protocol):
    def add_pending(self, blob: NewBlob) -> None: ...

    def find_ready_by_id(self, blob_id: UUID) -> BlobRecord | None: ...

    def find_ready_by_ids(self, blob_ids: Sequence[UUID]) -> dict[UUID, BlobRecord]: ...

    def find_ready_by_sha256(self, sha256: str) -> BlobRecord | None: ...

    def find_ready_by_sha256_many(self, sha256s: Sequence[str]) -> dict[str, BlobRecord]: ...

    def mark_ready(self, blob_id: UUID, expected_version: int, ready_at: datetime) -> bool: ...

    def mark_ready_many(
        self,
        expected: Sequence[tuple[UUID, int]],
        ready_at: datetime,
    ) -> int: ...

    def mark_quarantined(self, blob_id: UUID, expected_version: int) -> bool: ...

    def mark_quarantined_many(self, expected: Sequence[tuple[UUID, int]]) -> int: ...

    def prepare_deletions(self, jobs: Sequence[ClaimedGcJob]) -> list[PreparedGcJob]: ...

    def mark_deleted(
        self,
        jobs: Sequence[PreparedGcJob],
        deleted_at: datetime,
    ) -> int: ...


class GcJobRepository(Protocol):
    def schedule(self, job_id: UUID, blob_id: UUID, available_at: datetime) -> bool: ...

    def schedule_many(
        self,
        jobs: Sequence[tuple[UUID, UUID]],
        available_at: datetime,
    ) -> int: ...

    def claim_batch(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
    ) -> list[ClaimedGcJob]: ...

    def mark_completed(
        self,
        jobs: Sequence[ClaimedGcJob],
        *,
        worker_id: str,
        completed_at: datetime,
    ) -> int: ...

    def mark_failed(
        self,
        jobs: Sequence[FailedGcJob],
        *,
        worker_id: str,
        failed_at: datetime,
        max_attempts: int,
        base_retry_delay: timedelta,
        max_retry_delay: timedelta,
    ) -> int: ...


class AuditRepository(Protocol):
    def add(self, entry: NewAuditEntry) -> None: ...


class KernelMaintenanceReader(Protocol):
    def snapshot(self) -> KernelMaintenanceSnapshot: ...


class BlobStore(Protocol):
    def put(self, object_key: str, body: bytes | BinaryIO, media_type: str) -> None: ...

    def head(self, object_key: str) -> BlobObjectHead: ...

    def get_range(self, object_key: str, start: int, end_inclusive: int) -> bytes: ...

    def delete(self, object_key: str) -> None: ...


class EventPublisher(Protocol):
    def publish(self, event: ClaimedOutboxEvent) -> None: ...


class UnitOfWork(Protocol):
    outbox: OutboxRepository
    inbox: InboxRepository
    blobs: BlobCatalogRepository
    gc_jobs: GcJobRepository
    audit: AuditRepository

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
