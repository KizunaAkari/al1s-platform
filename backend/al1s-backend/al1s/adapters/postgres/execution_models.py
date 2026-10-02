from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class TerminalRegistrationGrantRow(Base):
    __tablename__ = "terminal_registration_grants"
    __table_args__ = (
        CheckConstraint("row_version > 0", name="ck_terminal_registration_grants_version"),
        CheckConstraint(
            "allowed_terminal_type IS NULL OR allowed_terminal_type IN ('linux', 'android')",
            name="ck_terminal_registration_grants_type",
        ),
        CheckConstraint(
            "target_device_id IS NULL OR allowed_terminal_type = 'android'",
            name="ck_terminal_registration_grants_target_type",
        ),
        Index("ix_terminal_registration_grants_expires", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    secret_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    allowed_terminal_type: Mapped[str | None] = mapped_column(String(32))
    target_device_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "target_devices.id",
            name="fk_terminal_registration_grants_target_device",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_by_terminal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "terminals.id",
            name="fk_terminal_registration_grants_consumed_terminal",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class TerminalRow(Base):
    __tablename__ = "terminals"
    __table_args__ = (
        CheckConstraint("terminal_type IN ('linux', 'android')", name="ck_terminals_type"),
        CheckConstraint("service_status IN ('online', 'offline')", name="ck_terminals_service"),
        CheckConstraint(
            "acceptance_status IN ('accepting', 'draining', 'disabled')",
            name="ck_terminals_acceptance",
        ),
        CheckConstraint("row_version > 0", name="ck_terminals_version"),
        CheckConstraint("name_version >= 0", name="ck_terminals_name_version"),
        CheckConstraint(
            "storage_total_bytes IS NULL OR (storage_total_bytes > 0 AND storage_used_bytes >= 0 "
            "AND storage_available_bytes >= 0 AND "
            "storage_used_bytes + storage_available_bytes <= storage_total_bytes)",
            name="ck_terminals_storage",
        ),
        Index("ix_terminals_status", "deleted_at", "service_status", "acceptance_status"),
        Index("ix_terminals_heartbeat_deadline", "last_seen_at", "id",
              postgresql_where=text("deleted_at IS NULL AND service_status = 'online'")),
        Index(
            "uq_terminals_active_installation",
            "installation_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    installation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    terminal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_is_custom: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    name_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    service_status: Mapped[str] = mapped_column(String(32), nullable=False, default="offline")
    acceptance_status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepting")
    agent_version: Mapped[str] = mapped_column(String(64), nullable=False)
    current_capability_profile_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "terminal_capability_profiles.id",
            name="fk_terminals_current_capability_profile",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    storage_directory: Mapped[str | None] = mapped_column(String(1024))
    storage_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    storage_used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    storage_available_bytes: Mapped[int | None] = mapped_column(BigInteger)
    storage_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    storage_probe_ok: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    storage_alert_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class TerminalCredentialRow(Base):
    __tablename__ = "terminal_credentials"
    __table_args__ = (
        CheckConstraint("row_version > 0", name="ck_terminal_credentials_version"),
        Index("ix_terminal_credentials_terminal", "terminal_id", "created_at"),
        Index(
            "uq_terminal_credentials_active",
            "terminal_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    terminal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    secret_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TerminalCapabilityProfileRow(Base):
    __tablename__ = "terminal_capability_profiles"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_terminal_capabilities_revision"),
        CheckConstraint("schema_version > 0", name="ck_terminal_capabilities_schema"),
        CheckConstraint("protocol_version > 0", name="ck_terminal_capabilities_protocol"),
        CheckConstraint("cpu_cores > 0", name="ck_terminal_capabilities_cpu"),
        CheckConstraint("memory_bytes >= 0", name="ck_terminal_capabilities_memory"),
        CheckConstraint("storage_available_bytes >= 0", name="ck_terminal_capabilities_storage"),
        CheckConstraint("manifest_hash ~ '^[0-9a-f]{64}$'", name="ck_terminal_capabilities_hash"),
        Index("ix_terminal_capabilities_terminal_created", "terminal_id", "created_at"),
        Index("ix_terminal_capabilities_match", "architecture", "accelerator_type"),
        Index("uq_terminal_capabilities_revision", "terminal_id", "revision", unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    terminal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_version: Mapped[str] = mapped_column(String(64), nullable=False)
    os_name: Mapped[str] = mapped_column(String(64), nullable=False)
    os_version: Mapped[str] = mapped_column(String(128), nullable=False)
    architecture: Mapped[str] = mapped_column(String(64), nullable=False)
    cpu_cores: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_available_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    accelerator_type: Mapped[str | None] = mapped_column(String(64))
    low_resource: Mapped[bool] = mapped_column(Boolean, nullable=False)
    provider_keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class TargetDeviceRow(Base):
    __tablename__ = "target_devices"
    __table_args__ = (
        CheckConstraint("platform = 'android'", name="ck_target_devices_platform"),
        CheckConstraint(
            "mode IN ('unassigned', 'standalone', 'mounted')", name="ck_target_devices_mode"
        ),
        CheckConstraint(
            "(mode = 'unassigned' AND managing_terminal_id IS NULL) OR "
            "(mode <> 'unassigned' AND managing_terminal_id IS NOT NULL)",
            name="ck_target_devices_manager",
        ),
        CheckConstraint("row_version > 0", name="ck_target_devices_version"),
        Index("ix_target_devices_manager", "managing_terminal_id", "deleted_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="android")
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="unassigned")
    managing_terminal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT")
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TargetDeviceIdentifierRow(Base):
    __tablename__ = "target_device_identifiers"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('apk_installation', 'adb_serial', 'android_id')",
            name="ck_target_identifiers_source",
        ),
        CheckConstraint(
            "identifier_digest ~ '^[0-9a-f]{64}$'", name="ck_target_identifiers_digest"
        ),
        CheckConstraint("row_version > 0", name="ck_target_identifiers_version"),
        CheckConstraint(
            "adb_state IS NULL OR (source_type = 'adb_serial' "
            "AND adb_state IN ('device','offline','unauthorized','other'))",
            name="ck_target_identifiers_adb_state",
        ),
        Index("ix_target_identifiers_target", "target_device_id", "deleted_at"),
        Index("ix_target_identifiers_source_terminal", "source_terminal_id", "created_at"),
        Index(
            "ix_target_identifiers_presence", "target_device_id", "observed_at",
            postgresql_where=text("deleted_at IS NULL AND source_type = 'adb_serial'"),
        ),
        Index(
            "uq_target_identifiers_active_source",
            "source_type",
            "identifier_digest",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_target_identifiers_active_apk_target",
            "target_device_id",
            unique=True,
            postgresql_where=text(
                "deleted_at IS NULL AND source_type = 'apk_installation' "
                "AND target_device_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    target_device_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("target_devices.id", ondelete="RESTRICT")
    )
    source_terminal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    identifier_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    display_hint: Mapped[str] = mapped_column(String(32), nullable=False)
    adb_state: Mapped[str | None] = mapped_column(String(24))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutionLeaseRow(Base):
    __tablename__ = "execution_leases"
    __table_args__ = (
        CheckConstraint(
            "terminal_id IS NOT NULL OR target_device_id IS NOT NULL",
            name="ck_execution_leases_resource",
        ),
        CheckConstraint(
            "lease_kind IN ('execution', 'remote_control', 'quick_test', 'maintenance')",
            name="ck_execution_leases_kind",
        ),
        CheckConstraint(
            "owner_kind IN "
            "('execution', 'execution_attempt', 'remote_session', 'quick_test', 'system')",
            name="ck_execution_leases_owner",
        ),
        CheckConstraint("expires_at > acquired_at", name="ck_execution_leases_expiry"),
        CheckConstraint("row_version > 0", name="ck_execution_leases_version"),
        Index("ix_execution_leases_expiry", "released_at", "expires_at"),
        Index("ix_execution_leases_owner", "owner_kind", "owner_id"),
        Index(
            "uq_execution_leases_active_terminal",
            "terminal_id",
            unique=True,
            postgresql_where=text("released_at IS NULL AND terminal_id IS NOT NULL"),
        ),
        Index(
            "uq_execution_leases_active_target",
            "target_device_id",
            unique=True,
            postgresql_where=text("released_at IS NULL AND target_device_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    terminal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT")
    )
    target_device_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("target_devices.id", ondelete="RESTRICT")
    )
    lease_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
