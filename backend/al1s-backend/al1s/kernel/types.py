from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class BlobStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    QUARANTINED = "quarantined"
    DELETING = "deleting"
    DELETED = "deleted"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PUBLISHED = "published"
    DEAD_LETTER = "dead_letter"


class GcJobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class NewOutboxEvent:
    event_id: UUID
    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: UUID
    correlation_id: UUID
    occurred_at: datetime
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    event_id: UUID
    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: UUID
    correlation_id: UUID
    occurred_at: datetime
    payload: dict[str, Any]
    attempt_count: int
    row_version: int


@dataclass(frozen=True, slots=True)
class FailedDelivery:
    event_id: UUID
    row_version: int
    attempt_count: int
    error_type: str


@dataclass(frozen=True, slots=True)
class NewBlob:
    blob_id: UUID
    sha256: str
    size_bytes: int
    media_type: str
    object_key: str


@dataclass(frozen=True, slots=True)
class BlobRecord:
    blob_id: UUID
    sha256: str
    size_bytes: int
    media_type: str
    object_key: str
    status: BlobStatus
    row_version: int
    created_at: datetime
    ready_at: datetime | None
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class ClaimedGcJob:
    job_id: UUID
    blob_id: UUID
    object_key: str
    blob_status: BlobStatus
    blob_row_version: int
    attempt_count: int
    row_version: int


@dataclass(frozen=True, slots=True)
class PreparedGcJob:
    job_id: UUID
    blob_id: UUID
    object_key: str
    blob_row_version: int
    attempt_count: int
    job_row_version: int


@dataclass(frozen=True, slots=True)
class FailedGcJob:
    job_id: UUID
    row_version: int
    attempt_count: int
    error_type: str


@dataclass(frozen=True, slots=True)
class NewAuditEntry:
    audit_id: UUID
    actor_type: str
    actor_id: UUID | None
    action: str
    target_type: str
    target_id: UUID | None
    correlation_id: UUID
    details: dict[str, Any]
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class KernelMaintenanceSnapshot:
    pending_outbox: int
    processing_outbox: int
    dead_letter_outbox: int
    oldest_pending_outbox_at: datetime | None
    pending_gc_jobs: int
    processing_gc_jobs: int
    dead_letter_gc_jobs: int
    pending_blobs: int
    quarantined_blobs: int
    reclaimable_blobs: int = 0
    reclaimable_bytes: int = 0


@dataclass(frozen=True, slots=True)
class BlobObjectHead:
    size_bytes: int
    media_type: str
    etag: str | None


@dataclass(frozen=True, slots=True)
class DispatchBatchResult:
    claimed: int
    published: int
    failed: int
    stale: int


@dataclass(frozen=True, slots=True)
class GcBatchResult:
    claimed: int
    deleted: int
    failed: int
    stale: int
