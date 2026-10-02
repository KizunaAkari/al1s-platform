from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class TerminalMqttSessionRow(Base):
    __tablename__ = "terminal_mqtt_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'active', 'revoked', 'expired')",
            name="ck_terminal_mqtt_sessions_status",
        ),
        CheckConstraint("expires_at > issued_at", name="ck_terminal_mqtt_sessions_expiry"),
        CheckConstraint("row_version > 0", name="ck_terminal_mqtt_sessions_version"),
        CheckConstraint(
            "(status = 'pending' AND configured_at IS NULL AND revoked_at IS NULL) OR "
            "(status = 'active' AND configured_at IS NOT NULL AND revoked_at IS NULL) OR "
            "(status IN ('revoked', 'expired') AND revoked_at IS NOT NULL)",
            name="ck_terminal_mqtt_sessions_lifecycle",
        ),
        Index(
            "ix_terminal_mqtt_sessions_terminal_status",
            "terminal_id",
            "status",
            "expires_at",
            "id",
        ),
        Index("ix_terminal_mqtt_sessions_expiry", "status", "expires_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    terminal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    client_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    role_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    password_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    configured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
