"""Create platform-kernel persistence tables.

Revision ID: 20260828_0002
Revises: 20260828_0001
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260828_0002"
down_revision: str | None = "20260828_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blob_objects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status <> 'deleted') OR deleted_at IS NOT NULL",
            name="ck_blob_objects_deleted_at",
        ),
        sa.CheckConstraint(
            "(status <> 'ready') OR ready_at IS NOT NULL",
            name="ck_blob_objects_ready_at",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_blob_objects_row_version"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_blob_objects_sha256"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_blob_objects_size_bytes"),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'quarantined', 'deleting', 'deleted')",
            name="ck_blob_objects_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index(
        "ix_blob_objects_sha256_status", "blob_objects", ["sha256", "status"], unique=False
    )
    op.create_index(
        "ix_blob_objects_status_created", "blob_objects", ["status", "created_at"], unique=False
    )
    op.create_index(
        "uq_blob_objects_ready_sha256",
        "blob_objects",
        ["sha256"],
        unique=True,
        postgresql_where=sa.text("status = 'ready'"),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(length=255), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_outbox_events_attempt_count"),
        sa.CheckConstraint(
            "(status <> 'processing') OR (locked_by IS NOT NULL AND locked_until IS NOT NULL)",
            name="ck_outbox_events_processing_lease",
        ),
        sa.CheckConstraint(
            "(status <> 'published') OR published_at IS NOT NULL",
            name="ck_outbox_events_published_at",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_outbox_events_row_version"),
        sa.CheckConstraint("schema_version > 0", name="ck_outbox_events_schema_version"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'published', 'dead_letter')",
            name="ck_outbox_events_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_events_aggregate",
        "outbox_events",
        ["aggregate_type", "aggregate_id"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_events_dispatch",
        "outbox_events",
        ["status", "available_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_events_expired_lease",
        "outbox_events",
        ["status", "locked_until"],
        unique=False,
    )

    op.create_table(
        "inbox_receipts",
        sa.Column("consumer_name", sa.String(length=255), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("consumer_name", "event_id"),
    )
    op.create_index(
        "ix_inbox_receipts_received_at", "inbox_receipts", ["received_at"], unique=False
    )

    op.create_table(
        "gc_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(length=255), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_count >= 0", name="ck_gc_jobs_attempt_count"),
        sa.CheckConstraint(
            "(status <> 'completed') OR completed_at IS NOT NULL",
            name="ck_gc_jobs_completed_at",
        ),
        sa.CheckConstraint(
            "(status <> 'processing') OR (locked_by IS NOT NULL AND locked_until IS NOT NULL)",
            name="ck_gc_jobs_processing_lease",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_gc_jobs_row_version"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'dead_letter')",
            name="ck_gc_jobs_status",
        ),
        sa.ForeignKeyConstraint(["blob_id"], ["blob_objects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_gc_jobs_dispatch", "gc_jobs", ["status", "available_at", "created_at"], unique=False
    )
    op.create_index("ix_gc_jobs_expired_lease", "gc_jobs", ["status", "locked_until"], unique=False)
    op.create_index(
        "uq_gc_jobs_active_blob",
        "gc_jobs",
        ["blob_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_type", sa.String(length=100), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=255), nullable=False),
        sa.Column("target_type", sa.String(length=100), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_correlation_id", "audit_logs", ["correlation_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index(
        "ix_audit_logs_target", "audit_logs", ["target_type", "target_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_target", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_correlation_id", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("uq_gc_jobs_active_blob", table_name="gc_jobs")
    op.drop_index("ix_gc_jobs_expired_lease", table_name="gc_jobs")
    op.drop_index("ix_gc_jobs_dispatch", table_name="gc_jobs")
    op.drop_table("gc_jobs")
    op.drop_index("ix_inbox_receipts_received_at", table_name="inbox_receipts")
    op.drop_table("inbox_receipts")
    op.drop_index("ix_outbox_events_expired_lease", table_name="outbox_events")
    op.drop_index("ix_outbox_events_dispatch", table_name="outbox_events")
    op.drop_index("ix_outbox_events_aggregate", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("uq_blob_objects_ready_sha256", table_name="blob_objects")
    op.drop_index("ix_blob_objects_status_created", table_name="blob_objects")
    op.drop_index("ix_blob_objects_sha256_status", table_name="blob_objects")
    op.drop_table("blob_objects")
