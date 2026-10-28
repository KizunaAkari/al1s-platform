"""Typed persistence for QQ paper accounts, market data and command settings."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base
from al1s.adapters.postgres.bot_models import (
    BotServiceRow as BotServiceRow,  # Register FK metadata.
)

MONEY = Numeric(20, 4)
PRICE = Numeric(20, 6)
RATE = Numeric(12, 8)
TIME = DateTime(timezone=True)


class QuantSettingsRow(Base):
    __tablename__ = "quant_settings"
    __table_args__ = (
        CheckConstraint("initial_cash > 0 AND row_version > 0", name="ck_quant_settings"),
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_services.id", ondelete="RESTRICT"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    private_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    event_ws_url: Mapped[str | None] = mapped_column(String(2048))
    initial_cash: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("100000"))
    commission_rate: Mapped[Decimal] = mapped_column(RATE, default=Decimal(0))
    minimum_commission: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    stamp_tax_rate: Mapped[Decimal] = mapped_column(RATE, default=Decimal(0))
    slippage: Mapped[Decimal] = mapped_column(RATE, default=Decimal(0))
    quote_max_age_seconds: Mapped[int] = mapped_column(Integer, default=120)
    bar_cache_days: Mapped[int] = mapped_column(Integer, default=30)
    refresh_seconds: Mapped[int] = mapped_column(Integer, default=60)
    reply_interval_seconds: Mapped[int] = mapped_column(Integer, default=10)
    provider: Mapped[str] = mapped_column(String(32), default="sina")
    provider_order: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        default=lambda: ["sina", "tencent", "eastmoney", "tushare", "gugudata"],
    )
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    mapping_version: Mapped[int] = mapped_column(Integer, default=1)


class QuantProviderCredentialRow(Base):
    __tablename__ = "quant_provider_credentials"
    __table_args__ = (
        CheckConstraint("provider IN ('tushare','gugudata')", name="ck_quant_credential_provider"),
    )
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    key_id: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(TIME)


class QuantScopeRow(Base):
    __tablename__ = "quant_scopes"
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("quant_settings.service_id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[str] = mapped_column(String(20), primary_key=True)


class QuantCommandRow(Base):
    __tablename__ = "quant_commands"
    __table_args__ = (
        UniqueConstraint("service_id", "normalized_keyword", name="uq_quant_command_keyword"),
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("quant_settings.service_id", ondelete="CASCADE"), primary_key=True
    )
    action: Mapped[str] = mapped_column(String(16), primary_key=True)
    keyword: Mapped[str] = mapped_column(String(32))
    normalized_keyword: Mapped[str] = mapped_column(String(32))


class QuantSendGateRow(Base):
    __tablename__ = "quant_send_gates"
    __table_args__ = (CheckConstraint("platform = 'qq'", name="ck_quant_send_platform"),)
    platform: Mapped[str] = mapped_column(String(8), primary_key=True)
    next_allowed_at: Mapped[datetime | None] = mapped_column(TIME)
    last_started_at: Mapped[datetime | None] = mapped_column(TIME)


class QuantReplyRow(Base):
    __tablename__ = "quant_replies"
    __table_args__ = (
        UniqueConstraint(
            "service_id",
            "scope_kind",
            "target_id",
            "sender_id",
            "message_id",
            "event_time",
            name="uq_quant_reply_event",
        ),
        CheckConstraint(
            "status IN ('queued','sending','sent','unknown')", name="ck_quant_reply_status"
        ),
        CheckConstraint("interval_seconds BETWEEN 1 AND 3600", name="ck_quant_reply_interval"),
        Index("ix_quant_reply_queue", "status", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id", ondelete="RESTRICT"))
    scope_kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(20))
    sender_id: Mapped[str] = mapped_column(String(20))
    message_id: Mapped[str] = mapped_column(String(64))
    event_time: Mapped[int] = mapped_column(BigInteger)
    request_sha256: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))
    mapping_version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(String(4096))
    interval_seconds: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(TIME)
    updated_at: Mapped[datetime] = mapped_column(TIME)


class QuantAccountRow(Base):
    __tablename__ = "quant_accounts"
    __table_args__ = (
        UniqueConstraint("qq_user_id", name="uq_quant_account_qq"),
        CheckConstraint(
            "cash >= 0 AND initial_cash > 0 AND version > 0", name="ck_quant_account_cash"
        ),
        CheckConstraint("state IN ('active','needs_review')", name="ck_quant_account_state"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    qq_user_id: Mapped[str] = mapped_column(String(20))
    initial_cash: Mapped[Decimal] = mapped_column(MONEY)
    cash: Mapped[Decimal] = mapped_column(MONEY)
    state: Mapped[str] = mapped_column(String(16), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    engine_version: Mapped[str | None] = mapped_column(String(32))
    engine_state: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(TIME)
    updated_at: Mapped[datetime] = mapped_column(TIME)


class QuantInstrumentRow(Base):
    __tablename__ = "quant_instruments"
    __table_args__ = (
        UniqueConstraint("market", "current_code", name="uq_quant_instrument_current"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    market: Mapped[str] = mapped_column(String(4))
    current_code: Mapped[str] = mapped_column(String(12))
    board: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    corporate_checked_at: Mapped[datetime | None] = mapped_column(TIME)


class QuantInstrumentCodeRow(Base):
    __tablename__ = "quant_instrument_codes"
    __table_args__ = (
        UniqueConstraint("market", "code", "valid_from", name="uq_quant_instrument_code"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("quant_instruments.id", ondelete="RESTRICT")
    )
    market: Mapped[str] = mapped_column(String(4))
    code: Mapped[str] = mapped_column(String(12))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)


class QuantPositionRow(Base):
    __tablename__ = "quant_positions"
    __table_args__ = (
        UniqueConstraint("account_id", "instrument_id", name="uq_quant_position"),
        CheckConstraint("quantity >= 0 AND total_cost >= 0", name="ck_quant_position_amount"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("quant_accounts.id", ondelete="RESTRICT"))
    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("quant_instruments.id", ondelete="RESTRICT")
    )
    quantity: Mapped[int] = mapped_column(BigInteger)
    total_cost: Mapped[Decimal] = mapped_column(MONEY)
    updated_at: Mapped[datetime] = mapped_column(TIME)


class QuantLotRow(Base):
    __tablename__ = "quant_lots"
    __table_args__ = (
        CheckConstraint(
            "remaining_quantity >= 0 AND remaining_cost >= 0", name="ck_quant_lot_amount"
        ),
        Index("ix_quant_lot_available", "position_id", "available_on", "acquired_on"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    position_id: Mapped[UUID] = mapped_column(ForeignKey("quant_positions.id", ondelete="RESTRICT"))
    acquired_on: Mapped[date] = mapped_column(Date)
    available_on: Mapped[date] = mapped_column(Date)
    remaining_quantity: Mapped[int] = mapped_column(BigInteger)
    remaining_cost: Mapped[Decimal] = mapped_column(MONEY)


class QuantOrderRow(Base):
    __tablename__ = "quant_orders"
    __table_args__ = (
        UniqueConstraint(
            "service_id",
            "scope_kind",
            "target_id",
            "sender_id",
            "message_id",
            "event_time",
            name="uq_quant_order_event",
        ),
        CheckConstraint("side IN ('buy','sell') AND quantity > 0", name="ck_quant_order_input"),
        CheckConstraint(
            "status IN ('accepted','processing','partially_filled',"
            "'filled','rejected','expired','unknown')",
            name="ck_quant_order_status",
        ),
        Index("ix_quant_order_history", "account_id", "created_at", "id"),
        Index("ix_quant_order_instrument_event", "instrument_id", "event_time", "account_id"),
        Index(
            "ix_quant_order_pending",
            "created_at",
            "id",
            postgresql_where=text("status = 'accepted'"),
        ),
        Index(
            "uq_quant_order_active",
            "account_id",
            unique=True,
            postgresql_where=text(
                "status IN ('accepted','processing','partially_filled','unknown')"
            ),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("quant_accounts.id", ondelete="RESTRICT"))
    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("quant_instruments.id", ondelete="RESTRICT")
    )
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id", ondelete="RESTRICT"))
    scope_kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(20))
    sender_id: Mapped[str] = mapped_column(String(20))
    message_id: Mapped[str] = mapped_column(String(64))
    event_time: Mapped[int] = mapped_column(BigInteger)
    request_sha256: Mapped[str] = mapped_column(String(64))
    mapping_version: Mapped[int] = mapped_column(Integer)
    settings_version: Mapped[int] = mapped_column(Integer)
    commission_rate: Mapped[Decimal] = mapped_column(RATE)
    minimum_commission: Mapped[Decimal] = mapped_column(MONEY)
    stamp_tax_rate: Mapped[Decimal] = mapped_column(RATE)
    slippage: Mapped[Decimal] = mapped_column(RATE)
    provider: Mapped[str] = mapped_column(String(32))
    provider_order: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        default=lambda: ["sina", "tencent", "eastmoney", "tushare", "gugudata"],
    )
    execution_provider: Mapped[str | None] = mapped_column(String(32))
    quote_max_age_seconds: Mapped[int] = mapped_column(Integer)
    refresh_seconds: Mapped[int] = mapped_column(Integer)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    quote_time: Mapped[datetime | None] = mapped_column(TIME)
    quote_price: Mapped[Decimal | None] = mapped_column(PRICE)
    created_at: Mapped[datetime] = mapped_column(TIME)
    updated_at: Mapped[datetime] = mapped_column(TIME)


class QuantFillRow(Base):
    __tablename__ = "quant_fills"
    __table_args__ = (
        UniqueConstraint("order_id", "sequence", name="uq_quant_fill_sequence"),
        CheckConstraint("quantity > 0 AND price > 0 AND fees >= 0", name="ck_quant_fill_amount"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("quant_orders.id", ondelete="RESTRICT"))
    sequence: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(BigInteger)
    price: Mapped[Decimal] = mapped_column(PRICE)
    fees: Mapped[Decimal] = mapped_column(MONEY)
    occurred_at: Mapped[datetime] = mapped_column(TIME)


class QuantCashFlowRow(Base):
    __tablename__ = "quant_cash_flows"
    __table_args__ = (
        Index("ix_quant_cash_history", "account_id", "occurred_at", "id"),
        UniqueConstraint("fill_id", "kind", name="uq_quant_cash_fill_kind"),
        UniqueConstraint("entitlement_id", "kind", name="uq_quant_cash_entitlement_kind"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("quant_accounts.id", ondelete="RESTRICT"))
    order_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("quant_orders.id", ondelete="RESTRICT")
    )
    fill_id: Mapped[UUID | None] = mapped_column(ForeignKey("quant_fills.id", ondelete="RESTRICT"))
    entitlement_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "quant_entitlements.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_quant_cash_entitlement",
        )
    )
    kind: Mapped[str] = mapped_column(String(24))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    occurred_at: Mapped[datetime] = mapped_column(TIME)


class QuantCorporateEventRow(Base):
    __tablename__ = "quant_corporate_events"
    __table_args__ = (
        UniqueConstraint("instrument_id", "canonical_event", name="uq_quant_corporate_event"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("quant_instruments.id", ondelete="RESTRICT")
    )
    canonical_event: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16))
    record_date: Mapped[date | None] = mapped_column(Date)
    ex_date: Mapped[date | None] = mapped_column(Date)
    cash_date: Mapped[date | None] = mapped_column(Date)
    shares_date: Mapped[date | None] = mapped_column(Date)
    cash_per_share: Mapped[Decimal] = mapped_column(PRICE)
    shares_per_share: Mapped[Decimal] = mapped_column(PRICE)
    source: Mapped[str] = mapped_column(String(32))
    captured_at: Mapped[datetime | None] = mapped_column(TIME)


class QuantEntitlementRow(Base):
    __tablename__ = "quant_entitlements"
    __table_args__ = (
        UniqueConstraint("account_id", "event_id", name="uq_quant_entitlement"),
        Index("ix_quant_entitlement_event", "event_id", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("quant_accounts.id", ondelete="RESTRICT"))
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("quant_corporate_events.id", ondelete="RESTRICT")
    )
    eligible_quantity: Mapped[int] = mapped_column(BigInteger)
    cash_applied_at: Mapped[datetime | None] = mapped_column(TIME)
    shares_applied_at: Mapped[datetime | None] = mapped_column(TIME)
    state: Mapped[str] = mapped_column(String(16))


class QuantQuoteRow(Base):
    __tablename__ = "quant_quotes"
    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("quant_instruments.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    price: Mapped[Decimal] = mapped_column(PRICE)
    quote_time: Mapped[datetime] = mapped_column(TIME)
    fetched_at: Mapped[datetime] = mapped_column(TIME)


class QuantBarRow(Base):
    __tablename__ = "quant_bars"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "provider", "frequency", "adjustment", "bar_time", name="uq_quant_bar"
        ),
        Index("ix_quant_bar_cleanup", "frequency", "bar_time"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("quant_instruments.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(32))
    frequency: Mapped[str] = mapped_column(String(8))
    adjustment: Mapped[str] = mapped_column(String(8))
    bar_time: Mapped[datetime] = mapped_column(TIME)
    open: Mapped[Decimal] = mapped_column(PRICE)
    high: Mapped[Decimal] = mapped_column(PRICE)
    low: Mapped[Decimal] = mapped_column(PRICE)
    close: Mapped[Decimal] = mapped_column(PRICE)
    volume: Mapped[int] = mapped_column(BigInteger)
    fetched_at: Mapped[datetime] = mapped_column(TIME)


class QuantBacktestRow(Base):
    __tablename__ = "quant_backtests"
    __table_args__ = (
        Index("ix_quant_backtest_history", "account_id", "created_at", "id"),
        UniqueConstraint(
            "service_id",
            "scope_kind",
            "target_id",
            "sender_id",
            "message_id",
            "event_time",
            name="uq_quant_backtest_event",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("quant_accounts.id", ondelete="RESTRICT"))
    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("quant_instruments.id", ondelete="RESTRICT")
    )
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id", ondelete="RESTRICT"))
    scope_kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(20))
    sender_id: Mapped[str] = mapped_column(String(20))
    message_id: Mapped[str] = mapped_column(String(64))
    event_time: Mapped[int] = mapped_column(BigInteger)
    request_sha256: Mapped[str] = mapped_column(String(64))
    mapping_version: Mapped[int] = mapped_column(Integer)
    settings_version: Mapped[int] = mapped_column(Integer)
    strategy: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    frequency: Mapped[str] = mapped_column(String(8))
    adjustment: Mapped[str] = mapped_column(String(8))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    initial_cash: Mapped[Decimal] = mapped_column(MONEY)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    total_return: Mapped[Decimal | None] = mapped_column(RATE)
    max_drawdown: Mapped[Decimal | None] = mapped_column(RATE)
    execution_count: Mapped[int | None] = mapped_column(Integer)
    data_sha256: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(TIME)
    updated_at: Mapped[datetime] = mapped_column(TIME)
