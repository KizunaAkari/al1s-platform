from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base
from al1s.maa.types import ScriptStatus


class MaaApplicationRow(Base):
    __tablename__ = "maa_applications"
    __table_args__ = (
        CheckConstraint("char_length(package_name) BETWEEN 1 AND 255", name="ck_maa_app_package"),
        CheckConstraint("char_length(display_name) BETWEEN 1 AND 255", name="ck_maa_app_name"),
        CheckConstraint("row_version > 0", name="ck_maa_app_version"),
        CheckConstraint(
            "icon_png IS NULL OR octet_length(icon_png) <= 65536",
            name="ck_maa_app_icon_size",
        ),
        Index("ix_maa_app_list", "deleted_at", "display_name", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    package_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    icon_png: Mapped[bytes | None] = mapped_column(LargeBinary)


class MaaApplicationDeviceRow(Base):
    __tablename__ = "maa_application_devices"
    __table_args__ = (
        Index(
            "uq_maa_app_device_package",
            "device_id",
            "package_name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_maa_app_device_pair",
            "application_id",
            "device_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_maa_app_devices_app", "application_id", "deleted_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    application_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_applications.id", ondelete="RESTRICT"),
        nullable=False,
    )
    device_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("target_devices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    package_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MaaScriptRow(Base):
    __tablename__ = "maa_scripts"
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 255", name="ck_maa_scripts_name"),
        CheckConstraint(
            "char_length(normalized_name) BETWEEN 1 AND 255",
            name="ck_maa_scripts_normalized_name",
        ),
        CheckConstraint(
            "script_type IN ('standard', 'module_start', 'module_process', 'module_end')",
            name="ck_maa_scripts_type",
        ),
        CheckConstraint(
            "status IN ('validation_pending', 'active', 'retired')",
            name="ck_maa_scripts_status",
        ),
        CheckConstraint("row_version > 0", name="ck_maa_scripts_version"),
        Index(
            "uq_maa_scripts_active_name",
            "application_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_maa_scripts_active_start",
            "application_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND script_type = 'module_start'"),
        ),
        Index(
            "uq_maa_scripts_active_end",
            "application_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND script_type = 'module_end'"),
        ),
        Index("ix_maa_scripts_list", "application_id", "status", "normalized_name", "id"),
        Index("ix_maa_scripts_display_order", "application_id", "display_order", "id"),
        Index(
            "ix_maa_scripts_candidate",
            "candidate_version_id",
            postgresql_where=text("candidate_version_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    application_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("maa_applications.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    script_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ScriptStatus.VALIDATION_PENDING.value
    )
    current_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_script_versions.id", ondelete="RESTRICT", use_alter=True),
    )
    candidate_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_script_versions.id", ondelete="RESTRICT", use_alter=True),
    )
    display_order: Mapped[Decimal] = mapped_column(
        Numeric(), nullable=False, server_default=text("nextval('maa_script_display_order_seq')")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class MaaScriptVersionRow(Base):
    __tablename__ = "maa_script_versions"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_maa_script_versions_revision"),
        CheckConstraint("schema_version > 0", name="ck_maa_script_versions_schema"),
        CheckConstraint("manifest_hash ~ '^[0-9a-f]{64}$'", name="ck_maa_script_versions_hash"),
        UniqueConstraint("script_id", "revision", name="uq_maa_script_versions_revision"),
        Index("ix_maa_script_versions_list", "script_id", "revision", "id"),
        Index("ix_maa_script_versions_hash", "script_id", "manifest_hash", "revision"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    script_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("maa_scripts.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MaaScriptReferenceRow(Base):
    __tablename__ = "maa_script_references"
    __table_args__ = (
        CheckConstraint("step_index > 0", name="ck_maa_script_references_step"),
        Index(
            "ix_maa_script_references_target", "target_script_id", "source_version_id", "step_index"
        ),
    )
    source_version_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("maa_script_versions.id", ondelete="RESTRICT"), primary_key=True
    )
    step_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_script_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("maa_scripts.id", ondelete="RESTRICT"), nullable=False
    )


class MaaScriptVersionBlobRow(Base):
    __tablename__ = "maa_script_version_blobs"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_maa_script_blobs_ordinal"),
        CheckConstraint(
            "char_length(json_pointer) BETWEEN 1 AND 1024",
            name="ck_maa_script_blobs_pointer",
        ),
        CheckConstraint(
            "char_length(resource_role) BETWEEN 1 AND 64", name="ck_maa_script_blobs_role"
        ),
        UniqueConstraint("script_version_id", "ordinal", name="uq_maa_script_blobs_ordinal"),
        Index("ix_maa_script_blobs_blob", "blob_id", "script_version_id"),
    )

    script_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_script_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blob_objects.id", ondelete="RESTRICT"), nullable=False
    )
    json_pointer: Mapped[str] = mapped_column(String(1024), primary_key=True)
    resource_role: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class MaaScriptQualificationReceiptRow(Base):
    __tablename__ = "maa_script_qualification_receipts"
    __table_args__ = (
        CheckConstraint("manifest_hash ~ '^[0-9a-f]{64}$'", name="ck_maa_qualification_hash"),
        CheckConstraint(
            "kind IN ('static_check', 'quick_test', 'step_test')", name="ck_maa_qualification_kind"
        ),
        CheckConstraint("status IN ('passed', 'failed')", name="ck_maa_qualification_status"),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_maa_qualification_idempotency",
        ),
        CheckConstraint(
            "char_length(executor_version) BETWEEN 1 AND 100",
            name="ck_maa_qualification_executor",
        ),
        CheckConstraint(
            "(status = 'passed' AND error_code IS NULL) OR "
            "(status = 'failed' AND error_code IS NOT NULL)",
            name="ck_maa_qualification_error",
        ),
        CheckConstraint(
            "(kind = 'static_check' AND terminal_id IS NULL AND target_device_id IS NULL) OR "
            "(kind IN ('quick_test', 'step_test') AND terminal_id IS NOT NULL "
            "AND target_device_id IS NOT NULL)",
            name="ck_maa_qualification_execution_context",
        ),
        UniqueConstraint(
            "script_version_id",
            "kind",
            "idempotency_key",
            name="uq_maa_qualification_idempotency",
        ),
        Index(
            "ix_maa_qualification_gate",
            "script_version_id",
            "kind",
            "status",
            text("created_at DESC"),
            "id",
        ),
        Index(
            "ix_maa_qualification_terminal",
            "terminal_id",
            text("created_at DESC"),
            "id",
            postgresql_where=text("terminal_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    script_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_script_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    terminal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT")
    )
    target_device_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("target_devices.id", ondelete="RESTRICT")
    )
    executor_version: Mapped[str] = mapped_column(String(100), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    diagnostic: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MaaQuickTestSessionRow(Base):
    __tablename__ = "maa_quick_test_sessions"
    __table_args__ = (
        CheckConstraint("manifest_hash ~ '^[0-9a-f]{64}$'", name="ck_maa_quick_test_manifest_hash"),
        CheckConstraint(
            "definition_hash ~ '^[0-9a-f]{64}$'", name="ck_maa_quick_test_definition_hash"
        ),
        CheckConstraint(
            "status IN ('issued', 'claimed', 'completed', 'expired', 'cancelled')",
            name="ck_maa_quick_test_status",
        ),
        CheckConstraint(
            "char_length(request_idempotency_key) BETWEEN 1 AND 128",
            name="ck_maa_quick_test_idempotency",
        ),
        CheckConstraint("row_version > 0", name="ck_maa_quick_test_version"),
        CheckConstraint(
            "event_last_sequence BETWEEN 0 AND 1000", name="ck_maa_quick_test_event_sequence"
        ),
        CheckConstraint(
            "(status = 'issued' AND claimed_at IS NULL AND completed_at IS NULL "
            "AND qualification_receipt_id IS NULL AND started_at IS NULL) OR "
            "(status = 'claimed' AND claimed_at IS NOT NULL AND completed_at IS NULL "
            "AND qualification_receipt_id IS NULL) OR "
            "(status = 'completed' AND claimed_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND qualification_receipt_id IS NOT NULL) OR "
            "(status = 'expired' AND completed_at IS NOT NULL "
            "AND qualification_receipt_id IS NULL) OR "
            "(status = 'cancelled' AND claimed_at IS NULL AND completed_at IS NOT NULL "
            "AND qualification_receipt_id IS NULL)",
            name="ck_maa_quick_test_completion",
        ),
        UniqueConstraint(
            "script_version_id",
            "request_idempotency_key",
            name="uq_maa_quick_test_idempotency",
        ),
        UniqueConstraint("qualification_receipt_id", name="uq_maa_quick_test_receipt"),
        Index(
            "ix_maa_quick_test_terminal_pending",
            "terminal_id",
            "status",
            "expires_at",
            "id",
        ),
        Index("ix_maa_quick_test_expiry", "status", "expires_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    script_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("maa_scripts.id", ondelete="RESTRICT"), nullable=False
    )
    script_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_script_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    terminal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    target_device_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("target_devices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    request_idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qualification_receipt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_script_qualification_receipts.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_last_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class MaaQuickTestEventRow(Base):
    __tablename__ = "maa_quick_test_events"
    __table_args__ = (
        CheckConstraint("sequence BETWEEN 1 AND 1000", name="ck_quick_event_sequence"),
        CheckConstraint(
            "kind IN ('started','step_started','step_succeeded','step_failed','log')",
            name="ck_quick_event_kind",
        ),
        CheckConstraint(
            "step_number IS NULL OR step_number BETWEEN 1 AND 1000",
            name="ck_quick_event_step",
        ),
        Index("ix_quick_event_retention", "created_at"),
    )

    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_quick_test_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    step_number: Mapped[int | None] = mapped_column(Integer)
    code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MaaQuickTestBlobRow(Base):
    __tablename__ = "maa_quick_test_blobs"
    __table_args__ = (
        CheckConstraint(
            "char_length(resource_key) BETWEEN 1 AND 255",
            name="ck_maa_quick_test_blobs_resource_key",
        ),
        CheckConstraint(
            "char_length(role) BETWEEN 1 AND 80",
            name="ck_maa_quick_test_blobs_role",
        ),
        CheckConstraint("ordinal >= 0", name="ck_maa_quick_test_blobs_ordinal"),
        UniqueConstraint("session_id", "resource_key", name="uq_maa_quick_test_blobs_resource_key"),
        UniqueConstraint("session_id", "ordinal", name="uq_maa_quick_test_blobs_ordinal"),
        Index("ix_maa_quick_test_blobs_blob", "blob_id", "session_id"),
    )

    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_quick_test_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("blob_objects.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    resource_key: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(80), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class MaaStrategyRow(Base):
    __tablename__ = "maa_strategies"
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 255", name="ck_maa_strategies_name"),
        CheckConstraint(
            "char_length(normalized_name) BETWEEN 1 AND 255",
            name="ck_maa_strategies_normalized_name",
        ),
        CheckConstraint("status IN ('active', 'retired')", name="ck_maa_strategies_status"),
        CheckConstraint("row_version > 0", name="ck_maa_strategies_version"),
        Index(
            "uq_maa_strategies_active_name",
            "application_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_maa_strategies_list",
            "application_id",
            "status",
            "normalized_name",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    application_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_applications.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_strategy_versions.id", ondelete="RESTRICT", use_alter=True),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class MaaStrategyVersionRow(Base):
    __tablename__ = "maa_strategy_versions"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_maa_strategy_versions_revision"),
        CheckConstraint("schema_version > 0", name="ck_maa_strategy_versions_schema"),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'",
            name="ck_maa_strategy_versions_hash",
        ),
        UniqueConstraint("strategy_id", "revision", name="uq_maa_strategy_versions_revision"),
        UniqueConstraint("strategy_id", "manifest_hash", name="uq_maa_strategy_versions_hash"),
        Index("ix_maa_strategy_versions_list", "strategy_id", "revision", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    strategy_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_strategies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MaaStrategyModuleRow(Base):
    __tablename__ = "maa_strategy_modules"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_maa_strategy_modules_position"),
        CheckConstraint(
            "module_role IN ('start', 'process', 'end')",
            name="ck_maa_strategy_modules_role",
        ),
        CheckConstraint(
            "wait_after_ms >= 0 AND wait_after_ms <= 14400000",
            name="ck_maa_strategy_modules_wait",
        ),
        Index(
            "uq_maa_strategy_modules_start",
            "strategy_version_id",
            unique=True,
            postgresql_where=text("module_role = 'start'"),
        ),
        Index(
            "uq_maa_strategy_modules_end",
            "strategy_version_id",
            unique=True,
            postgresql_where=text("module_role = 'end'"),
        ),
        Index(
            "ix_maa_strategy_modules_script",
            "script_version_id",
            "strategy_version_id",
        ),
    )

    strategy_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_strategy_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    module_role: Mapped[str] = mapped_column(String(16), nullable=False)
    script_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maa_script_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    wait_after_ms: Mapped[int] = mapped_column(Integer, nullable=False)


class MaaImportBatchRow(Base):
    __tablename__ = "maa_import_batches"
    __table_args__ = (
        CheckConstraint("logical_sha256 ~ '^[0-9a-f]{64}$'", name="ck_maa_import_logical_hash"),
        CheckConstraint("archive_sha256 ~ '^[0-9a-f]{64}$'", name="ck_maa_import_archive_hash"),
        CheckConstraint(
            "status IN ('processing', 'completed', 'failed')", name="ck_maa_import_status"
        ),
        CheckConstraint(
            "script_count >= 0 AND application_count >= 0 "
            "AND resource_reference_count >= 0 AND unique_resource_count >= 0",
            name="ck_maa_import_counts",
        ),
        CheckConstraint("row_version > 0", name="ck_maa_import_version"),
        UniqueConstraint("logical_sha256", name="uq_maa_import_logical_hash"),
        Index("ix_maa_import_list", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    logical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_schema: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    script_count: Mapped[int] = mapped_column(Integer, nullable=False)
    application_count: Mapped[int] = mapped_column(Integer, nullable=False)
    resource_reference_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unique_resource_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    diagnostic: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    strategy_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("maa_strategies.id", ondelete="RESTRICT")
    )


class MaaImportItemRow(Base):
    __tablename__ = "maa_import_items"
    __table_args__ = (
        CheckConstraint("archive_ordinal > 0", name="ck_maa_import_items_ordinal"),
        CheckConstraint("status IN ('imported', 'rejected')", name="ck_maa_import_items_status"),
        UniqueConstraint("batch_id", "archive_ordinal", name="uq_maa_import_items_ordinal"),
        Index("ix_maa_import_items_page", "batch_id", "archive_ordinal", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    batch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("maa_import_batches.id", ondelete="RESTRICT"), nullable=False
    )
    archive_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    script_name: Mapped[str] = mapped_column(String(255), nullable=False)
    application_package: Mapped[str | None] = mapped_column(String(255))
    script_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("maa_scripts.id", ondelete="RESTRICT")
    )
    script_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("maa_script_versions.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    migration_code: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(100))
    diagnostic: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MaaMutationReceiptRow(Base):
    __tablename__ = "maa_mutation_receipts"
    __table_args__ = (
        CheckConstraint(
            "char_length(operation) BETWEEN 1 AND 255",
            name="ck_maa_mutation_operation",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_maa_mutation_idempotency",
        ),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_maa_mutation_request_hash"),
        CheckConstraint(
            "char_length(aggregate_type) BETWEEN 1 AND 64",
            name="ck_maa_mutation_aggregate_type",
        ),
        CheckConstraint(
            "response_status BETWEEN 200 AND 299",
            name="ck_maa_mutation_response_status",
        ),
        UniqueConstraint("operation", "idempotency_key", name="uq_maa_mutation_idempotency"),
        Index("ix_maa_mutation_expiry", "expires_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    operation: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
