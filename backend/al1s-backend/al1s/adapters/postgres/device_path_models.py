"""Typed connection observations and one current physical input authority."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class DevicePathRow(Base):
    __tablename__ = "device_paths"
    __table_args__ = (
        CheckConstraint("kind IN ('linux','android')", name="ck_device_paths_kind"),
        CheckConstraint("ready_count BETWEEN 0 AND 2", name="ck_device_paths_ready"),
        CheckConstraint("authority_protocol IN (0,1)", name="ck_device_paths_protocol"),
        CheckConstraint("row_version > 0", name="ck_device_paths_version"),
        Index("ix_device_paths_terminal", "terminal_id", "device_id"),
    )
    device_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("target_devices.id", ondelete="RESTRICT"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    terminal_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    instance_id: Mapped[UUID | None] = mapped_column(Uuid)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    control_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ready_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    authority_protocol: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class DeviceAuthorityRow(Base):
    __tablename__ = "device_authorities"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active','releasing','released')", name="ck_device_authority_state"
        ),
        CheckConstraint("generation > 0 AND row_version > 0", name="ck_device_authority_version"),
        Index("ix_device_authority_holder", "holder_terminal_id", "state"),
    )
    device_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("target_devices.id", ondelete="RESTRICT"), primary_key=True
    )
    holder_terminal_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("terminals.id", ondelete="RESTRICT")
    )
    instance_id: Mapped[UUID | None] = mapped_column(Uuid)
    requested_terminal_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("terminals.id", ondelete="RESTRICT")
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    require_ocr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
