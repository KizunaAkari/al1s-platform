from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class MaintenanceCommandRow(Base):
    __tablename__ = "maa_maintenance_commands"
    __table_args__ = (
        CheckConstraint(
            "(action = 'upgrade_container') = (release_id IS NOT NULL)",
            name="ck_maa_maintenance_release",
        ),
        CheckConstraint(
            "action IN ('restart_container','restart_host','upgrade_container')",
            name="ck_maa_maintenance_action",
        ),
        CheckConstraint(
            "state IN ('pending','accepted','executing','recovering','succeeded',"
            "'refused','expired','response_timeout','recovery_timeout','failed','cancelled')",
            name="ck_maa_maintenance_state",
        ),
        Index("ix_maa_maintenance_poll", "next_check_at", "id"),
        Index("ix_maa_maintenance_terminal", "terminal_id", "submitted_at", "id"),
        Index(
            "uq_maa_maintenance_active",
            "terminal_id",
            unique=True,
            postgresql_where=text("state IN ('pending','accepted','executing','recovering')"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    # Match migration 0031: immediate NO ACTION also prevents referenced deletion.
    terminal_id: Mapped[UUID] = mapped_column(ForeignKey("terminals.id"))
    action: Mapped[str] = mapped_column(String(32))
    release_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("linux_releases.id", ondelete="RESTRICT")
    )
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    remote_version: Mapped[int] = mapped_column(Integer, default=0)
    late_state: Mapped[str | None] = mapped_column(String(32))
    late_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
