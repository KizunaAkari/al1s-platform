from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base
from al1s.kernel.types import BlobStatus, GcJobStatus, OutboxStatus


class BlobObjectRow(Base):
    __tablename__ = "blob_objects"
    __table_args__ = (
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_blob_objects_sha256"),
        CheckConstraint("size_bytes >= 0", name="ck_blob_objects_size_bytes"),
        CheckConstraint(
            "status IN ('pending', 'ready', 'quarantined', 'deleting', 'deleted')",
            name="ck_blob_objects_status",
        ),
        CheckConstraint("row_version > 0", name="ck_blob_objects_row_version"),
        CheckConstraint(
            "(status <> 'ready') OR ready_at IS NOT NULL",
            name="ck_blob_objects_ready_at",
        ),
        CheckConstraint(
            "(status <> 'deleted') OR deleted_at IS NOT NULL",
            name="ck_blob_objects_deleted_at",
        ),
        Index("ix_blob_objects_status_created", "status", "created_at"),
        Index("ix_blob_objects_sha256_status", "sha256", "status"),
        Index(
            "uq_blob_objects_ready_sha256",
            "sha256",
            unique=True,
            postgresql_where=text("status = 'ready'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=BlobStatus.PENDING.value
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEventRow(Base):
    __tablename__ = "outbox_events"
    details_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner_module: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="unassigned"
    )
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="ck_outbox_events_schema_version"),
        CheckConstraint(
            "owner_module IN ('platform', 'maa', 'information', 'unassigned')",
            name="ck_outbox_events_owner_module",
        ),
        Index("ix_outbox_events_owner_dispatch", "owner_module", "status", "available_at"),
        Index("ix_outbox_events_retention", "owner_module", "published_at", "id",
              postgresql_where=text("status = 'published' AND details_purged_at IS NULL")),
        CheckConstraint("attempt_count >= 0", name="ck_outbox_events_attempt_count"),
        CheckConstraint("row_version > 0", name="ck_outbox_events_row_version"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'published', 'dead_letter')",
            name="ck_outbox_events_status",
        ),
        CheckConstraint(
            "(status <> 'processing') OR (locked_by IS NOT NULL AND locked_until IS NOT NULL)",
            name="ck_outbox_events_processing_lease",
        ),
        CheckConstraint(
            "(status <> 'published') OR published_at IS NOT NULL",
            name="ck_outbox_events_published_at",
        ),
        Index(
            "ix_outbox_events_dispatch",
            "status",
            "available_at",
            "created_at",
        ),
        Index("ix_outbox_events_expired_lease", "status", "locked_until"),
        Index("ix_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=OutboxStatus.PENDING.value
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(255))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String(255))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class InboxReceiptRow(Base):
    __tablename__ = "inbox_receipts"
    __table_args__ = (Index("ix_inbox_receipts_received_at", "received_at"),)

    consumer_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GcJobRow(Base):
    __tablename__ = "gc_jobs"
    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="ck_gc_jobs_attempt_count"),
        CheckConstraint("row_version > 0", name="ck_gc_jobs_row_version"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'dead_letter')",
            name="ck_gc_jobs_status",
        ),
        CheckConstraint(
            "(status <> 'processing') OR (locked_by IS NOT NULL AND locked_until IS NOT NULL)",
            name="ck_gc_jobs_processing_lease",
        ),
        CheckConstraint(
            "(status <> 'completed') OR completed_at IS NOT NULL",
            name="ck_gc_jobs_completed_at",
        ),
        Index("ix_gc_jobs_dispatch", "status", "available_at", "created_at"),
        Index("ix_gc_jobs_expired_lease", "status", "locked_until"),
        Index(
            "uq_gc_jobs_active_blob",
            "blob_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'processing')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blob_objects.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=GcJobStatus.PENDING.value
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(255))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String(255))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StorageGcRequestRow(Base):
    __tablename__ = "storage_gc_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_storage_gc_requests_status",
        ),
        CheckConstraint("attempt_count BETWEEN 0 AND 3", name="ck_storage_gc_requests_attempts"),
        CheckConstraint(
            "(status <> 'processing') OR locked_until IS NOT NULL",
            name="ck_storage_gc_requests_lease",
        ),
        Index(
            "uq_storage_gc_requests_active", text("(1)"), unique=True,
            postgresql_where=text("status IN ('pending', 'processing')"),
        ),
        Index("ix_storage_gc_requests_requested", "requested_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stale: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_type: Mapped[str | None] = mapped_column(String(100))


class AuditLogRow(Base):
    __tablename__ = "audit_logs"
    details_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner_module: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="unassigned"
    )
    __table_args__ = (
        Index("ix_audit_logs_created_at", "created_at"),
        CheckConstraint(
            "owner_module IN ('platform', 'maa', 'information', 'unassigned')",
            name="ck_audit_logs_owner_module",
        ),
        Index("ix_audit_logs_owner_created", "owner_module", "created_at"),
        Index("ix_audit_logs_retention", "owner_module", "created_at", "id",
              postgresql_where=text("details_purged_at IS NULL")),
        Index("ix_audit_logs_target", "target_type", "target_id", "created_at"),
        Index("ix_audit_logs_correlation_id", "correlation_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    actor_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    target_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    summary: Mapped[str | None] = mapped_column(Text)
