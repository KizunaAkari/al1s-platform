from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class TaskPackageRow(Base):
    __tablename__ = "task_packages"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_task_packages_attempt"),
        CheckConstraint("protocol_version > 0", name="ck_task_packages_protocol"),
        CheckConstraint("package_schema_version > 0", name="ck_task_packages_schema"),
        CheckConstraint("snapshot_schema_version > 0", name="ck_task_packages_snapshot_schema"),
        CheckConstraint("delivery_attempt_count > 0", name="ck_task_packages_delivery_count"),
        CheckConstraint("row_version > 0", name="ck_task_packages_version"),
        CheckConstraint(
            "status IN ('available', 'accepted', 'delivery_failed', 'cancelled')",
            name="ck_task_packages_status",
        ),
        CheckConstraint(
            "last_rejection_disposition IS NULL OR "
            "last_rejection_disposition IN "
            "('retryable_wait', 'blocked_wait', 'permanent_failure')",
            name="ck_task_packages_rejection_disposition",
        ),
        Index(
            "ix_task_packages_terminal_pending",
            "terminal_id",
            "status",
            "next_delivery_at",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("executions.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    terminal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    target_device_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("target_devices.id", ondelete="RESTRICT")
    )
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    package_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    delivery_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_delivery_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_rejection_disposition: Mapped[str | None] = mapped_column(String(32))
    last_rejection_code: Mapped[str | None] = mapped_column(String(64))
    last_rejection_diagnostic: Mapped[str | None] = mapped_column(String(512))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class TerminalCommandRow(Base):
    __tablename__ = "commands"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "command_kind", "delivery_no", name="uq_commands_attempt_delivery"
        ),
        CheckConstraint(
            "command_kind IN ('task_package_available', 'cancel_requested')",
            name="ck_commands_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'acknowledged', 'superseded')",
            name="ck_commands_status",
        ),
        CheckConstraint("delivery_no > 0", name="ck_commands_delivery_no"),
        CheckConstraint("row_version > 0", name="ck_commands_version"),
        Index(
            "ix_commands_terminal_pending",
            "terminal_id",
            "status",
            "available_at",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    terminal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    command_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    package_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task_packages.id", ondelete="RESTRICT")
    )
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    delivery_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CommandAcknowledgementRow(Base):
    __tablename__ = "command_acknowledgements"
    __table_args__ = (
        CheckConstraint(
            "outcome IN "
            "('running_cancel_accepted', 'already_completed', 'cancelled_before_start')",
            name="ck_command_acknowledgements_outcome",
        ),
        UniqueConstraint("command_id", name="uq_command_acknowledgements_command"),
        Index("ix_command_acknowledgements_attempt", "attempt_id", "received_at"),
    )

    terminal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("terminals.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    report_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    command_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("commands.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_attempts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("executions.id", ondelete="RESTRICT"), nullable=False
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TerminalReportRow(Base):
    __tablename__ = "terminal_reports"
    __table_args__ = (
        CheckConstraint(
            "report_kind IN ('package_receipt', 'attempt_start', 'attempt_result')",
            name="ck_terminal_reports_kind",
        ),
        CheckConstraint(
            "disposition IN ('accepted', 'duplicate', 'stale', 'rejected')",
            name="ck_terminal_reports_disposition",
        ),
        Index("ix_terminal_reports_attempt", "attempt_id", "received_at"),
        Index("ix_terminal_reports_received", "received_at"),
    )

    terminal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT"), primary_key=True
    )
    report_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    report_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    command_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("commands.id", ondelete="RESTRICT")
    )
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    package_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task_packages.id", ondelete="RESTRICT")
    )
    lease_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_leases.id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    diagnostic: Mapped[str | None] = mapped_column(String(512))
    result_diagnostic: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    detail_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OfflineStartPermitRow(Base):
    __tablename__ = "offline_start_permits"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_offline_start_permits_attempt"),
        UniqueConstraint("token_digest", name="uq_offline_start_permits_token_digest"),
        CheckConstraint("permit_version > 0", name="ck_offline_start_permits_protocol"),
        CheckConstraint("row_version > 0", name="ck_offline_start_permits_version"),
        CheckConstraint(
            "status IN ('issued', 'consumed', 'revoked', 'expired')",
            name="ck_offline_start_permits_status",
        ),
        CheckConstraint(
            "expires_at > issued_at", name="ck_offline_start_permits_expiry"
        ),
        CheckConstraint(
            "(status = 'issued' AND consumed_at IS NULL "
            "AND consumed_start_report_id IS NULL AND revoked_at IS NULL) OR "
            "(status = 'consumed' AND consumed_at IS NOT NULL "
            "AND consumed_start_report_id IS NOT NULL AND revoked_at IS NULL) OR "
            "(status IN ('revoked', 'expired') AND consumed_at IS NULL "
            "AND consumed_start_report_id IS NULL AND revoked_at IS NOT NULL)",
            name="ck_offline_start_permits_lifecycle",
        ),
        Index(
            "ix_offline_start_permits_terminal_status",
            "terminal_id",
            "status",
            "issued_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    package_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task_packages.id", ondelete="RESTRICT"), nullable=False
    )
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("executions.id", ondelete="RESTRICT"), nullable=False
    )
    terminal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    target_device_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("target_devices.id", ondelete="RESTRICT")
    )
    package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    permit_version: Mapped[int] = mapped_column(Integer, nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_start_report_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
