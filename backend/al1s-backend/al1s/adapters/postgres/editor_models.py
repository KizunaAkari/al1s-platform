from datetime import datetime
from uuid import UUID

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class EditorSessionRow(Base):
    __tablename__ = "interactive_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','active','closing','closed','failed','expired')",
            name="ck_editor_status",
        ),
        CheckConstraint("row_version > 0", name="ck_editor_version"),
        CheckConstraint("create_deadline > created_at", name="ck_editor_deadline"),
        CheckConstraint("(ciphertext IS NULL) = (key_id IS NULL)", name="ck_editor_secret_pair"),
        CheckConstraint(
            "status NOT IN ('closed','failed','expired') OR ciphertext IS NULL",
            name="ck_editor_final_no_secret",
        ),
        CheckConstraint(
            "status <> 'active' OR (ciphertext IS NOT NULL AND terminal_instance_id IS NOT NULL)",
            name="ck_editor_active_secret",
        ),
        Index(
            "uq_editor_active_device",
            "device_id",
            unique=True,
            postgresql_where=text("status IN ('pending','active','closing')"),
        ),
        Index("ix_editor_terminal_pending", "terminal_id", "status", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    terminal_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("terminals.id", ondelete="RESTRICT"))
    device_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("target_devices.id", ondelete="RESTRICT")
    )
    request_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    create_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    ciphertext: Mapped[str | None] = mapped_column(Text)
    key_id: Mapped[str | None] = mapped_column(String(16))
    terminal_instance_id: Mapped[UUID | None] = mapped_column(Uuid)
    error_code: Mapped[str | None] = mapped_column(String(64))
