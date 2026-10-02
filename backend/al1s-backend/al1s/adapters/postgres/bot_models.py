from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class BotServiceRow(Base):
    __tablename__ = "bot_services"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('onebot_gateway', 'discord_bridge')", name="ck_bot_services_kind"
        ),
        CheckConstraint("row_version > 0", name="ck_bot_services_row_version"),
        Index("ix_bot_services_kind_created", "kind", "created_at", "id"),
        Index("ix_bot_services_created", "created_at", "id"),
        Index(
            "uq_bot_services_active_name",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    desired_config_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "bot_config_versions.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_bot_services_desired_config",
        ),
        nullable=True,
    )
    applied_config_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "bot_config_versions.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_bot_services_applied_config",
        ),
        nullable=True,
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BotRegistrationGrantRow(Base):
    __tablename__ = "bot_registration_grants"
    __table_args__ = (
        CheckConstraint("row_version > 0", name="ck_bot_registration_grants_row_version"),
        Index("ix_bot_registration_grants_service_created", "service_id", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_services.id", ondelete="RESTRICT"), nullable=False
    )
    secret_digest: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_by_identity_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "bot_worker_identities.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_bot_registration_grants_consumed_identity",
        ),
        nullable=True,
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class BotWorkerIdentityRow(Base):
    __tablename__ = "bot_worker_identities"
    __table_args__ = (
        CheckConstraint("credential_version > 0", name="ck_bot_worker_identity_version"),
        UniqueConstraint("service_id", name="uq_bot_worker_identity_service"),
        Index("ix_bot_worker_identity_enabled", "enabled", "last_seen_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_services.id", ondelete="RESTRICT"), nullable=False
    )
    credential_digest: Mapped[str] = mapped_column(Text, nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BotConfigVersionRow(Base):
    __tablename__ = "bot_config_versions"
    __table_args__ = (
        CheckConstraint("version_no > 0", name="ck_bot_config_versions_version"),
        UniqueConstraint("service_id", "version_no", name="uq_bot_config_service_version"),
        Index("ix_bot_config_versions_service_created", "service_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_services.id", ondelete="RESTRICT"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    secret_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("encrypted_secrets.id", ondelete="RESTRICT")
    )
    onebot_service_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("bot_services.id", ondelete="RESTRICT")
    )
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class BotConfigApplicationRow(Base):
    __tablename__ = "bot_config_applications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'applied', 'rejected')",
            name="ck_bot_config_applications_status",
        ),
        CheckConstraint("row_version > 0", name="ck_bot_config_applications_row_version"),
        CheckConstraint(
            "status = 'pending' OR completed_at IS NOT NULL",
            name="ck_bot_config_applications_completed",
        ),
        UniqueConstraint("config_version_id", name="uq_bot_config_application_version"),
        UniqueConstraint("receipt_event_id", name="uq_bot_config_application_receipt"),
        Index(
            "ix_bot_config_applications_service_status",
            "service_id",
            "status",
            "requested_at",
        ),
        Index(
            "ix_bot_config_applications_service_requested",
            "service_id",
            "requested_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_services.id", ondelete="RESTRICT"), nullable=False
    )
    config_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_config_versions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    worker_instance_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_summary: Mapped[str | None] = mapped_column(String(500))
    receipt_event_id: Mapped[UUID | None] = mapped_column(nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class BotHealthReportRow(Base):
    __tablename__ = "bot_health_reports"
    __table_args__ = (
        CheckConstraint(
            "status IN ('healthy', 'degraded', 'unhealthy')",
            name="ck_bot_health_reports_status",
        ),
        UniqueConstraint("receipt_event_id", name="uq_bot_health_receipt"),
        Index("ix_bot_health_service_reported", "service_id", "reported_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    receipt_event_id: Mapped[UUID] = mapped_column(nullable=False)
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_services.id", ondelete="RESTRICT"), nullable=False
    )
    identity_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_worker_identities.id", ondelete="RESTRICT"), nullable=False
    )
    config_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("bot_config_versions.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    diagnostics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
