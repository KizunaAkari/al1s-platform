from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class EncryptedSecretRow(Base):
    __tablename__ = "encrypted_secrets"
    __table_args__ = (
        CheckConstraint("row_version > 0", name="ck_encrypted_secrets_row_version"),
        Index("ix_encrypted_secrets_purpose", "purpose"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    purpose: Mapped[str] = mapped_column(String(255), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class NotificationChannelRow(Base):
    __tablename__ = "notification_channels"
    __table_args__ = (
        CheckConstraint("kind IN ('smtp', 'qq', 'discord')", name="ck_notification_channels_kind"),
        CheckConstraint(
            "(kind = 'smtp' AND bot_service_id IS NULL) OR "
            "(kind IN ('qq', 'discord') AND bot_service_id IS NOT NULL AND secret_id IS NULL)",
            name="ck_notification_channels_bot_binding",
        ),
        CheckConstraint("row_version > 0", name="ck_notification_channels_row_version"),
        Index("ix_notification_channels_kind_created", "kind", "created_at", "id"),
        Index("ix_notification_channels_created", "created_at", "id"),
        Index("ix_notification_channels_bot_service", "bot_service_id"),
        Index(
            "uq_notification_channels_active_name",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    secret_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("encrypted_secrets.id", ondelete="RESTRICT")
    )
    bot_service_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "bot_services.id",
            ondelete="RESTRICT",
            name="fk_notification_channels_bot_service",
        ),
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationRouteRow(Base):
    __tablename__ = "notification_routes"
    __table_args__ = (
        CheckConstraint(
            "notification_kind IN "
            "('conditional_skip', 'script_failure', 'storage_low', 'test', 'forward', "
            "'terminal_alert', 'operation_failure')",
            name="ck_notification_routes_kind",
        ),
        CheckConstraint("row_version > 0", name="ck_notification_routes_row_version"),
        UniqueConstraint(
            "notification_kind", "channel_id", "template_key", name="uq_notification_route_shape"
        ),
        Index(
            "ix_notification_routes_active_kind",
            "notification_kind",
            "enabled",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_notification_routes_created", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    notification_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    channel_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("notification_channels.id", ondelete="RESTRICT"),
        nullable=False,
    )
    targets: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    template_key: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationIntentRow(Base):
    __tablename__ = "notification_intents"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="ck_notification_intents_schema_version"),
        CheckConstraint(
            "notification_kind IN "
            "('conditional_skip', 'script_failure', 'storage_low', 'test', 'forward', "
            "'terminal_alert', 'operation_failure')",
            name="ck_notification_intents_kind",
        ),
        CheckConstraint(
            "disposition IN ('queued', 'no_route')", name="ck_notification_intents_disposition"
        ),
        Index("ix_notification_intents_created", "created_at", "id"),
        Index("ix_notification_intents_source", "source_type", "source_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    notification_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    source_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class NotificationDeliveryRow(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint(
            "channel_kind IN ('smtp', 'qq', 'discord')",
            name="ck_notification_deliveries_channel_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'dead_letter', 'cancelled')",
            name="ck_notification_deliveries_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_notification_deliveries_attempt_count"),
        CheckConstraint("row_version > 0", name="ck_notification_deliveries_row_version"),
        CheckConstraint(
            "status <> 'processing' OR (locked_by IS NOT NULL AND locked_until IS NOT NULL)",
            name="ck_notification_deliveries_processing_lease",
        ),
        CheckConstraint(
            "status <> 'sent' OR sent_at IS NOT NULL",
            name="ck_notification_deliveries_sent_at",
        ),
        UniqueConstraint(
            "intent_id",
            "route_id",
            "channel_id",
            "target_key",
            name="uq_notification_delivery_target",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("jsonb_array_length(targets) = 1", name="ck_notification_single_target"),
        Index("ix_notification_deliveries_dispatch", "status", "available_at", "created_at"),
        Index("ix_notification_deliveries_expired", "status", "locked_until"),
        Index("ix_notification_deliveries_intent", "intent_id"),
        Index("ix_notification_deliveries_channel_created", "channel_id", "created_at"),
        Index("ix_notification_deliveries_created", "created_at", "id"),
        Index(
            "ix_notification_deliveries_status_created",
            "status",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    intent_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("notification_intents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    route_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("notification_routes.id", ondelete="RESTRICT")
    )
    channel_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("notification_channels.id", ondelete="RESTRICT"),
        nullable=False,
    )
    channel_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_key: Mapped[str] = mapped_column(
        Text, Computed("targets ->> 0", persisted=True), nullable=False
    )
    targets: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    template_key: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(255))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String(100))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationAttemptRow(Base):
    __tablename__ = "notification_attempts"
    __table_args__ = (
        CheckConstraint("attempt_no > 0", name="ck_notification_attempts_attempt_no"),
        CheckConstraint("outcome IN ('sent', 'failed')", name="ck_notification_attempts_outcome"),
        UniqueConstraint("delivery_id", "attempt_no", name="uq_notification_attempt_number"),
        Index("ix_notification_attempts_delivery", "delivery_id", "attempt_no"),
        Index("ix_notification_attempts_started", "started_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("notification_deliveries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(100))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
