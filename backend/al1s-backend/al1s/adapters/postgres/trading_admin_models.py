from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base
from al1s.adapters.postgres.bot_models import BotServiceRow as BotServiceRow


class TradingAdminOwnerRow(Base):
    __tablename__ = "trading_admin_owner"
    __table_args__ = (CheckConstraint("id = 1 AND row_version > 0", name="ck_trading_admin_owner"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    qq_id: Mapped[str] = mapped_column(String(20), nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TradingAdminOperationRow(Base):
    __tablename__ = "trading_admin_operations"
    __table_args__ = (
        UniqueConstraint("service_id", "key_hash", name="uq_trading_admin_operation_key"),
        CheckConstraint("market IN ('a-share','spot','futures')", name="ck_trading_admin_market"),
        CheckConstraint("status IN ('running','completed','failed','unknown')", name="ck_trading_admin_status"),
        Index("ix_trading_admin_owner_time", "qq_id", "created_at", "id"),
        Index("uq_trading_admin_pending", "qq_id", "market", unique=True,
              postgresql_where=text("status IN ('running','unknown')")),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id", ondelete="RESTRICT"), nullable=False)
    qq_id: Mapped[str] = mapped_column(String(20), nullable=False)
    market: Mapped[str] = mapped_column(String(12), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    dispatch_started: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    account_protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    protected_version: Mapped[int | None] = mapped_column(Integer)
    event_time: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
