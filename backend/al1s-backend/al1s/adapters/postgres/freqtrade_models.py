from datetime import datetime
from decimal import Decimal
from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base
from al1s.adapters.postgres.bot_models import BotServiceRow as BotServiceRow


class FreqtradeSettingsRow(Base):
    __tablename__ = "freqtrade_settings"
    __table_args__ = (
        CheckConstraint(
            "initial_cash > 0 AND row_version > 0 AND fee >= 0 AND fee <= 1",
            name="ck_freqtrade_settings",
        ),
        CheckConstraint(
            "futures_initial_cash > 0 AND futures_fee >= 0 AND futures_fee <= 1 "
            "AND futures_default_leverage BETWEEN 1 AND 150 "
            "AND (futures_max_leverage = 0 OR futures_default_leverage <= futures_max_leverage) "
            "AND futures_max_leverage BETWEEN 0 AND 150",
            name="ck_freqtrade_futures_settings",
        ),
    )
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    private_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    event_ws_url: Mapped[str | None] = mapped_column(String(1024))
    groups: Mapped[list[str]] = mapped_column(ARRAY(String(20)), default=list)
    initial_cash: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    fee: Mapped[Decimal] = mapped_column(Numeric(12, 8))
    reply_interval_seconds: Mapped[int] = mapped_column(Integer)
    max_active_accounts: Mapped[int] = mapped_column(Integer)
    idle_seconds: Mapped[int] = mapped_column(Integer)
    row_version: Mapped[int] = mapped_column(Integer)
    futures_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    futures_initial_cash: Mapped[Decimal] = mapped_column(
        Numeric(20, 4), default=1000, server_default="1000"
    )
    futures_fee: Mapped[Decimal] = mapped_column(
        Numeric(12, 8), default=Decimal("0.0005"), server_default="0.0005"
    )
    futures_default_leverage: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    futures_max_leverage: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class FreqtradeWordRow(Base):
    __tablename__ = "freqtrade_commands"
    __table_args__ = (
        UniqueConstraint("service_id", "normalized_keyword", name="uq_freqtrade_word"),
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("freqtrade_settings.service_id"), primary_key=True
    )
    action: Mapped[str] = mapped_column(String(16), primary_key=True)
    keyword: Mapped[str] = mapped_column(String(32))
    normalized_keyword: Mapped[str] = mapped_column(String(64))


class FreqtradeProfileRow(Base):
    market: ClassVar[str] = "spot"
    __tablename__ = "freqtrade_profiles"
    __table_args__ = (
        CheckConstraint(
            "state IN ('ready','unknown') AND initial_cash > 0", name="ck_freqtrade_profile"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    qq_id: Mapped[str] = mapped_column(String(20), unique=True)
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id"))
    initial_cash: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    state: Mapped[str] = mapped_column(String(16), default="ready")
    native_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FreqtradeFuturesProfileRow(Base):
    market: ClassVar[str] = "futures"
    __tablename__ = "freqtrade_futures_profiles"
    __table_args__ = (
        CheckConstraint(
            "state IN ('ready','unknown') AND initial_cash > 0", name="ck_freqtrade_futures_profile"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    qq_id: Mapped[str] = mapped_column(String(20), unique=True)
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id"))
    initial_cash: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    state: Mapped[str] = mapped_column(String(16), default="ready")
    native_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notices_since: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    exit_scan_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FreqtradeRequestRow(Base):
    __tablename__ = "freqtrade_requests"
    __table_args__ = (
        UniqueConstraint(
            "service_id",
            "scope_kind",
            "target_id",
            "sender_id",
            "message_id",
            "event_time",
            name="uq_freqtrade_event",
        ),
        CheckConstraint(
            "status IN ('processing','completed','unknown')", name="ck_freqtrade_request"
        ),
        Index("ix_freqtrade_request_owner", "sender_id", "created_at", "id"),
        Index(
            "ix_freqtrade_request_market_owner", "status", "market", "sender_id", "created_at", "id"
        ),
        CheckConstraint("market IN ('spot','futures')", name="ck_freqtrade_request_market"),
        CheckConstraint("capital_amount IS NULL OR (capital_amount > 0 AND capital_amount <= 1000000000)",
                        name="ck_freqtrade_capital_amount"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id"))
    scope_kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(20))
    sender_id: Mapped[str] = mapped_column(String(20))
    message_id: Mapped[str] = mapped_column(String(64))
    event_time: Mapped[int] = mapped_column()
    action: Mapped[str] = mapped_column(String(16))
    request_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    result: Mapped[str | None] = mapped_column(Text)
    native_trade_id: Mapped[int | None] = mapped_column(Integer)
    native_order_count: Mapped[int | None] = mapped_column(Integer)
    capital_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    market: Mapped[str] = mapped_column(String(8), default="spot", server_default="spot")
