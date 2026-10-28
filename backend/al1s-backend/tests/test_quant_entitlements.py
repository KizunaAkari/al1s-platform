from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.quant_entitlements import QuantEntitlements
from al1s.adapters.postgres.quant_models import (QuantAccountRow, QuantCashFlowRow, QuantCorporateEventRow,
    QuantEntitlementRow, QuantFillRow, QuantInstrumentRow, QuantLotRow, QuantOrderRow, QuantPositionRow)
from al1s.adapters.postgres.bot_models import BotServiceRow


def test_post_sale_entitlement_is_applied_once_without_a_trade():
    engine = create_engine("sqlite://")
    for table in (BotServiceRow.__table__, QuantAccountRow.__table__, QuantInstrumentRow.__table__,
                  QuantOrderRow.__table__, QuantFillRow.__table__, QuantPositionRow.__table__,
                  QuantLotRow.__table__, QuantCorporateEventRow.__table__, QuantEntitlementRow.__table__,
                  QuantCashFlowRow.__table__):
        table.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now, account_id, stock_id, event_id, right_id = datetime.now(UTC), uuid4(), uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(QuantAccountRow(id=account_id, qq_user_id="123", initial_cash=Decimal("100000"),
                                    cash=Decimal("100000"), state="active", version=1,
                                    engine_state=b"snapshot", engine_version="0.3.71", created_at=now, updated_at=now))
        session.add(QuantInstrumentRow(id=stock_id, market="SH", current_code="600000", board="main", name="test", active=True))
        session.add(QuantCorporateEventRow(id=event_id, instrument_id=stock_id, canonical_event="test",
            state="verified", record_date=date(2026, 1, 5), cash_date=date(2026, 1, 8),
            shares_date=date(2026, 1, 8), cash_per_share=Decimal("0.5"), shares_per_share=Decimal("0.1"), source="cninfo"))
        session.add(QuantEntitlementRow(id=right_id, account_id=account_id, event_id=event_id,
                                      eligible_quantity=100, state="verified"))
    calls = []
    def adjust(state, symbol, shares, cash, applied_at):
        calls.append((state, symbol, shares, cash))
        return SimpleNamespace(state=b"updated", cash=Decimal("100050"), position=10, available=10)
    repo = QuantEntitlements(sessions, adjust)
    assert repo.apply(right_id, date(2026, 1, 8)) is True
    assert repo.apply(right_id, date(2026, 1, 8)) is False
    assert len(calls) == 1 and calls[0][2:] == (10, Decimal("50"))
    with sessions() as session:
        assert session.get(QuantAccountRow, account_id).cash == Decimal("100050")
        assert session.query(QuantPositionRow).one().quantity == 10
        assert session.query(QuantLotRow).one().remaining_cost == 0
        assert session.query(QuantCashFlowRow).one().entitlement_id == right_id
        assert session.query(QuantFillRow).count() == 0


def test_record_date_snapshot_uses_history_even_if_holder_later_sells():
    engine = create_engine("sqlite://")
    for table in (BotServiceRow.__table__, QuantAccountRow.__table__, QuantInstrumentRow.__table__,
                  QuantOrderRow.__table__, QuantFillRow.__table__, QuantCorporateEventRow.__table__,
                  QuantEntitlementRow.__table__):
        table.create(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_quant_order_active")
    sessions = sessionmaker(engine, expire_on_commit=False)
    now, account, stock, event, service = datetime.now(UTC), uuid4(), uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(QuantAccountRow(id=account, qq_user_id="123", initial_cash=Decimal("100000"),
                                    cash=Decimal("100000"), state="active", version=1, created_at=now, updated_at=now))
        session.add(QuantInstrumentRow(id=stock, market="SH", current_code="600000", board="main", name="x", active=True))
        session.add(QuantCorporateEventRow(id=event, instrument_id=stock, canonical_event="event", state="verified",
            record_date=date(2026, 1, 5), cash_date=date(2026, 1, 8), shares_date=date(2026, 1, 8),
            cash_per_share=Decimal("0.5"), shares_per_share=Decimal("0.1"), source="cninfo"))
        for day, side in ((5, "buy"), (6, "sell")):
            order = uuid4()
            session.add(QuantOrderRow(id=order, account_id=account, instrument_id=stock, service_id=service,
                scope_kind="qq_group", target_id="1", sender_id="123", message_id=str(day), event_time=day,
                request_sha256="a"*64, mapping_version=1, settings_version=1, commission_rate=0,
                minimum_commission=0, stamp_tax_rate=0, slippage=0, provider="sina", quote_max_age_seconds=120, refresh_seconds=60, side=side,
                quantity=100, status="filled", created_at=now, updated_at=now))
            session.add(QuantFillRow(id=uuid4(), order_id=order, sequence=1, quantity=100, price=10,
                                     fees=0, occurred_at=datetime.fromisoformat(f"2026-01-0{day}T10:00:00+08:00")))
    repo = QuantEntitlements(sessions, lambda *args: None)
    assert repo.capture(event, date(2026, 1, 7)) == 1
    assert repo.capture(event, date(2026, 1, 7)) == 0
    with sessions() as session:
        assert session.query(QuantEntitlementRow).one().eligible_quantity == 100
