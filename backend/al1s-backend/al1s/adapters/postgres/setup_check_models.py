"""Actual setup-check completion is independent from terminal heartbeats."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class TerminalSetupCheckRow(Base):
    __tablename__ = "terminal_setup_checks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','claimed','completed')", name="ck_setup_check_status"
        ),
        CheckConstraint("source IN ('automatic','manual')", name="ck_setup_check_source"),
        CheckConstraint("row_version > 0", name="ck_setup_check_version"),
        Index(
            "uq_setup_check_pending",
            "terminal_id",
            unique=True,
            postgresql_where=text("status IN ('pending','claimed')"),
        ),
        Index("ix_setup_check_completed", "terminal_id", "completed_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    terminal_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    instance_id: Mapped[UUID | None] = mapped_column(Uuid)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    secure_lock: Mapped[bool | None] = mapped_column(Boolean)
    provider_ready: Mapped[bool | None] = mapped_column(Boolean)
    battery_exempt: Mapped[bool | None] = mapped_column(Boolean)
    notifications_allowed: Mapped[bool | None] = mapped_column(Boolean)
    wireless_recovery_enabled: Mapped[bool | None] = mapped_column(Boolean)
    manufacturer_policy: Mapped[str | None] = mapped_column(String(32))
    result_hash: Mapped[str | None] = mapped_column(String(64))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
