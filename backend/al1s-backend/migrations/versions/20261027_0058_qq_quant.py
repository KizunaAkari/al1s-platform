"""Add independent QQ quantitative accounts and market data."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261027_0058"
down_revision = "20261013_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quant_send_gates",
        sa.Column("platform", sa.String(length=8), nullable=False),
        sa.Column("next_allowed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("platform = 'qq'", name="ck_quant_send_platform"),
        sa.PrimaryKeyConstraint("platform"),
    )
    op.create_table(
        "quant_replies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.String(length=20), nullable=False),
        sa.Column("sender_id", sa.String(length=20), nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("event_time", sa.BigInteger(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("mapping_version", sa.Integer(), nullable=False),
        sa.Column("content", sa.String(length=4096), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','sending','sent','unknown')", name="ck_quant_reply_status"
        ),
        sa.CheckConstraint("interval_seconds BETWEEN 1 AND 3600", name="ck_quant_reply_interval"),
        sa.ForeignKeyConstraint(["service_id"], ["bot_services.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "service_id",
            "scope_kind",
            "target_id",
            "sender_id",
            "message_id",
            "event_time",
            name="uq_quant_reply_event",
        ),
    )
    op.create_index("ix_quant_reply_queue", "quant_replies", ["status", "created_at", "id"])
    op.create_table(
        "quant_settings",
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("private_enabled", sa.Boolean(), nullable=False),
        sa.Column("event_ws_url", sa.String(length=2048), nullable=True),
        sa.Column("initial_cash", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("commission_rate", sa.Numeric(precision=12, scale=8), nullable=False),
        sa.Column("minimum_commission", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("stamp_tax_rate", sa.Numeric(precision=12, scale=8), nullable=False),
        sa.Column("slippage", sa.Numeric(precision=12, scale=8), nullable=False),
        sa.Column("quote_max_age_seconds", sa.Integer(), nullable=False),
        sa.Column("bar_cache_days", sa.Integer(), nullable=False),
        sa.Column("refresh_seconds", sa.Integer(), nullable=False),
        sa.Column("reply_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("mapping_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("initial_cash > 0 AND row_version > 0", name="ck_quant_settings"),
        sa.ForeignKeyConstraint(["service_id"], ["bot_services.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("service_id"),
    )
    op.create_table(
        "quant_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("qq_user_id", sa.String(length=20), nullable=False),
        sa.Column("initial_cash", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("cash", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=True),
        sa.Column("engine_state", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('active','needs_review')", name="ck_quant_account_state"),
        sa.CheckConstraint(
            "cash >= 0 AND initial_cash > 0 AND version > 0", name="ck_quant_account_cash"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("qq_user_id", name="uq_quant_account_qq"),
    )
    op.create_table(
        "quant_instruments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("market", sa.String(length=4), nullable=False),
        sa.Column("current_code", sa.String(length=12), nullable=False),
        sa.Column("board", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("corporate_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market", "current_code", name="uq_quant_instrument_current"),
    )
    op.create_table(
        "quant_scopes",
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["service_id"], ["quant_settings.service_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("service_id", "group_id"),
    )
    op.create_table(
        "quant_commands",
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("keyword", sa.String(length=32), nullable=False),
        sa.Column("normalized_keyword", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["service_id"], ["quant_settings.service_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("service_id", "action"),
        sa.UniqueConstraint("service_id", "normalized_keyword", name="uq_quant_command_keyword"),
    )
    op.create_table(
        "quant_instrument_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("market", sa.String(length=4), nullable=False),
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["instrument_id"], ["quant_instruments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market", "code", "valid_from", name="uq_quant_instrument_code"),
    )
    op.create_table(
        "quant_positions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("total_cost", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity >= 0 AND total_cost >= 0", name="ck_quant_position_amount"),
        sa.ForeignKeyConstraint(["account_id"], ["quant_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["instrument_id"], ["quant_instruments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "instrument_id", name="uq_quant_position"),
    )
    op.create_table(
        "quant_lots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("position_id", sa.Uuid(), nullable=False),
        sa.Column("acquired_on", sa.Date(), nullable=False),
        sa.Column("available_on", sa.Date(), nullable=False),
        sa.Column("remaining_quantity", sa.BigInteger(), nullable=False),
        sa.Column("remaining_cost", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.CheckConstraint(
            "remaining_quantity >= 0 AND remaining_cost >= 0", name="ck_quant_lot_amount"
        ),
        sa.ForeignKeyConstraint(["position_id"], ["quant_positions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_quant_lot_available",
        "quant_lots",
        ["position_id", "available_on", "acquired_on"],
        unique=False,
    )
    op.create_table(
        "quant_orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.String(length=20), nullable=False),
        sa.Column("sender_id", sa.String(length=20), nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("event_time", sa.BigInteger(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("mapping_version", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("settings_version", sa.Integer(), nullable=False),
        sa.Column("commission_rate", sa.Numeric(precision=12, scale=8), nullable=False),
        sa.Column("minimum_commission", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("stamp_tax_rate", sa.Numeric(precision=12, scale=8), nullable=False),
        sa.Column("slippage", sa.Numeric(precision=12, scale=8), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("quote_max_age_seconds", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("refresh_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("quote_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quote_price", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("side IN ('buy','sell') AND quantity > 0", name="ck_quant_order_input"),
        sa.CheckConstraint(
            "status IN ('accepted','processing','partially_filled',"
            "'filled','rejected','expired','unknown')",
            name="ck_quant_order_status",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["quant_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["instrument_id"], ["quant_instruments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["service_id"], ["bot_services.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "service_id",
            "scope_kind",
            "target_id",
            "sender_id",
            "message_id",
            "event_time",
            name="uq_quant_order_event",
        ),
    )
    op.create_index(
        "ix_quant_order_history", "quant_orders", ["account_id", "created_at", "id"], unique=False
    )
    op.create_index(
        "ix_quant_order_instrument_event",
        "quant_orders",
        ["instrument_id", "event_time", "account_id"],
        unique=False,
    )
    op.create_index(
        "ix_quant_order_pending",
        "quant_orders",
        ["created_at", "id"],
        postgresql_where=sa.text("status = 'accepted'"),
    )
    op.create_index(
        "uq_quant_order_active",
        "quant_orders",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('accepted','processing','partially_filled','unknown')"
        ),
    )
    op.create_table(
        "quant_fills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("price", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("fees", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity > 0 AND price > 0 AND fees >= 0", name="ck_quant_fill_amount"),
        sa.ForeignKeyConstraint(["order_id"], ["quant_orders.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "sequence", name="uq_quant_fill_sequence"),
    )
    op.create_table(
        "quant_cash_flows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("fill_id", sa.Uuid(), nullable=True),
        sa.Column("entitlement_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["quant_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["quant_orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["fill_id"], ["quant_fills.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fill_id", "kind", name="uq_quant_cash_fill_kind"),
        sa.UniqueConstraint("entitlement_id", "kind", name="uq_quant_cash_entitlement_kind"),
    )
    op.create_index(
        "ix_quant_cash_history",
        "quant_cash_flows",
        ["account_id", "occurred_at", "id"],
        unique=False,
    )
    op.create_table(
        "quant_corporate_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_event", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("record_date", sa.Date(), nullable=True),
        sa.Column("ex_date", sa.Date(), nullable=True),
        sa.Column("cash_date", sa.Date(), nullable=True),
        sa.Column("shares_date", sa.Date(), nullable=True),
        sa.Column("cash_per_share", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("shares_per_share", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["instrument_id"], ["quant_instruments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "canonical_event", name="uq_quant_corporate_event"),
    )
    op.create_table(
        "quant_entitlements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("eligible_quantity", sa.BigInteger(), nullable=False),
        sa.Column("cash_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shares_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["quant_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["event_id"], ["quant_corporate_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "event_id", name="uq_quant_entitlement"),
    )
    op.create_foreign_key(
        "fk_quant_cash_entitlement",
        "quant_cash_flows",
        "quant_entitlements",
        ["entitlement_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_quant_entitlement_event", "quant_entitlements", ["event_id", "id"])
    op.create_table(
        "quant_quotes",
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("price", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("quote_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["quant_instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("instrument_id", "provider"),
    )
    op.create_table(
        "quant_bars",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("frequency", sa.String(length=8), nullable=False),
        sa.Column("adjustment", sa.String(length=8), nullable=False),
        sa.Column("bar_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["quant_instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id", "provider", "frequency", "adjustment", "bar_time", name="uq_quant_bar"
        ),
    )
    op.create_index("ix_quant_bar_cleanup", "quant_bars", ["frequency", "bar_time"], unique=False)
    op.create_table(
        "quant_backtests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.String(length=20), nullable=False),
        sa.Column("sender_id", sa.String(length=20), nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("event_time", sa.BigInteger(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("mapping_version", sa.Integer(), nullable=False),
        sa.Column("settings_version", sa.Integer(), nullable=False),
        sa.Column("strategy", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("frequency", sa.String(length=8), nullable=False),
        sa.Column("adjustment", sa.String(length=8), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("initial_cash", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("total_return", sa.Numeric(precision=12, scale=8), nullable=True),
        sa.Column("max_drawdown", sa.Numeric(precision=12, scale=8), nullable=True),
        sa.Column("execution_count", sa.Integer(), nullable=True),
        sa.Column("data_sha256", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["quant_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["instrument_id"], ["quant_instruments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["service_id"], ["bot_services.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "service_id",
            "scope_kind",
            "target_id",
            "sender_id",
            "message_id",
            "event_time",
            name="uq_quant_backtest_event",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_quant_backtest_history",
        "quant_backtests",
        ["account_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    raise RuntimeError("quant_data_requires_explicit_migration")
