"""Create the Stage 7A notification kernel.

Revision ID: 20260905_0019
Revises: 20260905_0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_0019"
down_revision: str | None = "20260905_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "encrypted_secrets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=255), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("row_version > 0", name="ck_encrypted_secrets_row_version"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_encrypted_secrets_purpose", "encrypted_secrets", ["purpose"])
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("secret_id", sa.Uuid(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('smtp', 'qq', 'discord')", name="ck_notification_channels_kind"
        ),
        sa.CheckConstraint("row_version > 0", name="ck_notification_channels_row_version"),
        sa.ForeignKeyConstraint(["secret_id"], ["encrypted_secrets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_channels_kind_created",
        "notification_channels",
        ["kind", "created_at", "id"],
    )
    op.create_index(
        "ix_notification_channels_created",
        "notification_channels",
        ["created_at", "id"],
    )
    op.create_index(
        "uq_notification_channels_active_name",
        "notification_channels",
        ["name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "notification_routes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("notification_kind", sa.String(length=50), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("targets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("template_key", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "notification_kind IN ('conditional_skip', 'script_failure', 'storage_low', 'test')",
            name="ck_notification_routes_kind",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_notification_routes_row_version"),
        sa.ForeignKeyConstraint(["channel_id"], ["notification_channels.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "notification_kind", "channel_id", "template_key", name="uq_notification_route_shape"
        ),
    )
    op.create_index(
        "ix_notification_routes_active_kind",
        "notification_routes",
        ["notification_kind", "enabled", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_notification_routes_created", "notification_routes", ["created_at", "id"]
    )
    op.create_table(
        "notification_intents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.Uuid(), nullable=False),
        sa.Column("notification_kind", sa.String(length=50), nullable=False),
        sa.Column("source_type", sa.String(length=100), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("schema_version > 0", name="ck_notification_intents_schema_version"),
        sa.CheckConstraint(
            "notification_kind IN ('conditional_skip', 'script_failure', 'storage_low', 'test')",
            name="ck_notification_intents_kind",
        ),
        sa.CheckConstraint(
            "disposition IN ('queued', 'no_route')", name="ck_notification_intents_disposition"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_event_id"),
    )
    op.create_index("ix_notification_intents_created", "notification_intents", ["created_at", "id"])
    op.create_index(
        "ix_notification_intents_source",
        "notification_intents",
        ["source_type", "source_id", "created_at"],
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=True),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("channel_kind", sa.String(length=32), nullable=False),
        sa.Column("targets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("template_key", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(length=100), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "channel_kind IN ('smtp', 'qq', 'discord')",
            name="ck_notification_deliveries_channel_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'dead_letter', 'cancelled')",
            name="ck_notification_deliveries_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_notification_deliveries_attempt_count"),
        sa.CheckConstraint("row_version > 0", name="ck_notification_deliveries_row_version"),
        sa.CheckConstraint(
            "status <> 'processing' OR (locked_by IS NOT NULL AND locked_until IS NOT NULL)",
            name="ck_notification_deliveries_processing_lease",
        ),
        sa.CheckConstraint(
            "status <> 'sent' OR sent_at IS NOT NULL", name="ck_notification_deliveries_sent_at"
        ),
        sa.ForeignKeyConstraint(["channel_id"], ["notification_channels.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["intent_id"], ["notification_intents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["route_id"], ["notification_routes.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("intent_id", "route_id", name="uq_notification_delivery_intent_route"),
    )
    op.create_index(
        "ix_notification_deliveries_dispatch",
        "notification_deliveries",
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_notification_deliveries_expired", "notification_deliveries", ["status", "locked_until"]
    )
    op.create_index("ix_notification_deliveries_intent", "notification_deliveries", ["intent_id"])
    op.create_index(
        "ix_notification_deliveries_channel_created",
        "notification_deliveries",
        ["channel_id", "created_at"],
    )
    op.create_index(
        "ix_notification_deliveries_created", "notification_deliveries", ["created_at", "id"]
    )
    op.create_index(
        "ix_notification_deliveries_status_created",
        "notification_deliveries",
        ["status", "created_at", "id"],
    )
    op.create_table(
        "notification_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("error_type", sa.String(length=100), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt_no > 0", name="ck_notification_attempts_attempt_no"),
        sa.CheckConstraint(
            "outcome IN ('sent', 'failed')", name="ck_notification_attempts_outcome"
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"], ["notification_deliveries.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_id", "attempt_no", name="uq_notification_attempt_number"),
    )
    op.create_index(
        "ix_notification_attempts_delivery", "notification_attempts", ["delivery_id", "attempt_no"]
    )
    op.create_index(
        "ix_notification_attempts_started", "notification_attempts", ["started_at", "id"]
    )


def downgrade() -> None:
    op.drop_table("notification_attempts")
    op.drop_table("notification_deliveries")
    op.drop_table("notification_intents")
    op.drop_table("notification_routes")
    op.drop_table("notification_channels")
    op.drop_table("encrypted_secrets")
