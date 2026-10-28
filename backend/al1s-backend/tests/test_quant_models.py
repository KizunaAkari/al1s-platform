from sqlalchemy import Numeric, String
import subprocess
import sys

from al1s.adapters.postgres.quant_models import (
    QuantAccountRow,
    QuantBacktestRow,
    QuantBarRow,
    QuantCommandRow,
    QuantCashFlowRow,
    QuantCorporateEventRow,
    QuantEntitlementRow,
    QuantLotRow,
    QuantOrderRow,
    QuantPositionRow,
    QuantReplyRow,
    QuantSendGateRow,
    QuantSettingsRow,
)


def unique_columns(table):
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }


def test_qq_accounts_and_multiple_positions_have_typed_unique_ownership():
    assert isinstance(QuantAccountRow.__table__.c.qq_user_id.type, String)
    assert ("qq_user_id",) in unique_columns(QuantAccountRow.__table__)
    assert isinstance(QuantAccountRow.__table__.c.cash.type, Numeric)
    assert ("account_id", "instrument_id") in unique_columns(QuantPositionRow.__table__)


def test_deduplication_is_scoped_to_the_actual_event_and_action():
    assert (
        "service_id",
        "scope_kind",
        "target_id",
        "sender_id",
        "message_id",
        "event_time",
    ) in unique_columns(QuantOrderRow.__table__)
    assert ("service_id", "normalized_keyword") in unique_columns(QuantCommandRow.__table__)
    assert ("account_id", "event_id") in unique_columns(QuantEntitlementRow.__table__)


def test_price_history_has_a_bounded_indexable_identity():
    assert ("instrument_id", "provider", "frequency", "adjustment", "bar_time") in unique_columns(
        QuantBarRow.__table__
    )
    assert isinstance(QuantBarRow.__table__.c.close.type, Numeric)


def test_quant_can_receive_without_enabling_meme_and_keeps_partial_orders_active():
    assert "event_ws_url" in QuantSettingsRow.__table__.c
    checks = " ".join(
        str(c.sqltext)
        for c in QuantOrderRow.__table__.constraints
        if c.__class__.__name__ == "CheckConstraint"
    )
    assert "partially_filled" in checks
    active = next(i for i in QuantOrderRow.__table__.indexes if i.name == "uq_quant_order_active")
    assert "partially_filled" in str(active.dialect_options["postgresql"]["where"])
    assert {"commission_rate", "minimum_commission", "stamp_tax_rate", "slippage", "provider", "settings_version"} <= set(QuantOrderRow.__table__.c.keys())


def test_lots_keep_remaining_total_cost_without_rounded_unit_cost_drift():
    assert "remaining_cost" in QuantLotRow.__table__.c
    assert isinstance(QuantLotRow.__table__.c.remaining_cost.type, Numeric)


def test_replies_have_their_own_global_gate_and_idempotent_event_identity():
    assert QuantSendGateRow.__tablename__ == "quant_send_gates"
    assert ("service_id", "scope_kind", "target_id", "sender_id", "message_id", "event_time") in unique_columns(
        QuantReplyRow.__table__
    )


def test_cash_ledger_links_fills_and_entitlements_for_idempotency():
    assert ("fill_id", "kind") in unique_columns(QuantCashFlowRow.__table__)
    assert ("entitlement_id", "kind") in unique_columns(QuantCashFlowRow.__table__)
    assert QuantCorporateEventRow.__table__.c.record_date.nullable
    assert "captured_at" in QuantCorporateEventRow.__table__.c


def test_backtests_deduplicate_the_authenticated_qq_event():
    assert ("service_id", "scope_kind", "target_id", "sender_id", "message_id", "event_time") in unique_columns(QuantBacktestRow.__table__)


def test_quant_model_resolves_bot_fk_in_a_fresh_process():
    code = "from al1s.adapters.postgres.quant_models import QuantOrderRow; assert next(iter(QuantOrderRow.__table__.c.service_id.foreign_keys)).column.table.name == 'bot_services'"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
