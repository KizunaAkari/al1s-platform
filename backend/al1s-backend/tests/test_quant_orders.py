from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.bot_models import BotServiceRow
from al1s.adapters.postgres.quant_accounts import QuantAccounts
from al1s.adapters.postgres.quant_models import (
    QuantAccountRow,
    QuantCashFlowRow,
    QuantFillRow,
    QuantInstrumentRow,
    QuantLotRow,
    QuantOrderRow,
    QuantPositionRow,
)
from al1s.adapters.postgres.quant_orders import QuantOrders
from al1s.meme_workshop.contracts import CommandSource
from al1s.quant.book import Fill
from al1s.quant.commands import QuantCommand
from al1s.quant.contracts import QuantSettings


@pytest.fixture
def setup():
    engine = create_engine("sqlite://")
    for table in (
        BotServiceRow.__table__,
        QuantAccountRow.__table__,
        QuantInstrumentRow.__table__,
        QuantOrderRow.__table__,
        QuantPositionRow.__table__,
        QuantLotRow.__table__,
        QuantFillRow.__table__,
        QuantCashFlowRow.__table__,
    ):
        table.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    accounts, orders = QuantAccounts(sessions), QuantOrders(sessions, enforce_corporate=False)
    account, _ = accounts.open("12345", Decimal("100000"))
    service_id, instrument_id = uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            BotServiceRow(
                id=service_id, kind="onebot_gateway", name="QQ", enabled=True, row_version=1
            )
        )
        session.add(
            QuantInstrumentRow(
                id=instrument_id,
                market="SH",
                current_code="600000",
                board="main",
                name="name",
                active=True,
            )
        )
    return orders, sessions, service_id, instrument_id, account


def command(message_id="1", quantity="100"):
    return QuantCommand(
        CommandSource(
            message_id=message_id, scope_kind="qq_group", target_id="999", sender_id="12345"
        ),
        "buy",
        ("600000", quantity),
        1,
        1790000000,
        settings_version=1,
        settings=QuantSettings(),
    )


def test_onebot_replay_is_idempotent_and_changed_payload_is_rejected(setup):
    orders, _, service, instrument, _ = setup
    first, created = orders.accept(service, command(), instrument, 100)
    again, created_again = orders.accept(service, command(), instrument, 100)
    assert created and not created_again and first.id == again.id and first.status == "accepted"
    with pytest.raises(ValueError, match="event_conflict"):
        orders.accept(service, command(quantity="200"), instrument, 200)


def test_unresolved_order_blocks_following_order_and_other_account(setup):
    orders, sessions, service, instrument, account = setup
    first, _ = orders.accept(service, command(), instrument, 100)
    with pytest.raises(ValueError, match="order_pending"):
        orders.accept(service, command(message_id="2"), instrument, 100)
    with sessions.begin() as session:
        row = session.get(QuantOrderRow, first.id)
        row.status = "unknown"
        session.get(QuantAccountRow, account.id).state = "needs_review"
    with pytest.raises(ValueError, match="needs_review"):
        orders.accept(service, command(message_id="3"), instrument, 100)


def test_processing_order_is_marked_unknown_on_restart_without_replay(setup):
    orders, sessions, service, instrument, account = setup
    first, _ = orders.accept(service, command(), instrument, 100)
    pending = orders.pending()
    assert len(pending) == 1 and pending[0].id == first.id and pending[0].symbol == "600000"
    snapshot = orders.prepare(first.id)
    assert snapshot.order.status == "processing" and snapshot.cash == Decimal("100000")
    assert snapshot.positions == {"600000": 0}
    assert orders.recover_interrupted() == 1
    with sessions() as session:
        assert session.get(QuantOrderRow, first.id).status == "unknown"
        assert session.get(QuantAccountRow, account.id).state == "needs_review"
    assert orders.recover_interrupted() == 0
    with pytest.raises(ValueError, match="needs_review"):
        orders.accept(service, command(message_id="new"), instrument, 100)


def test_unknown_execution_blocks_only_its_account(setup):
    orders, sessions, service, instrument, account = setup
    first, _ = orders.accept(service, command(), instrument, 100)
    orders.start(first.id)
    assert orders.mark_unknown(first.id)
    with sessions() as session:
        assert session.get(QuantOrderRow, first.id).status == "unknown"
        assert session.get(QuantAccountRow, account.id).state == "needs_review"
    assert not orders.mark_unknown(first.id)


def test_quote_and_execution_sources_are_persisted_once_before_engine(setup):
    orders, sessions, service, instrument, _ = setup
    order, _ = orders.accept(service, command(), instrument, 100, quote_provider="tencent")
    snapshot = orders.prepare(order.id, execution_provider="eastmoney")
    assert snapshot.order.provider == "tencent" and snapshot.order.execution_provider == "eastmoney"
    with sessions() as session:
        row = session.get(QuantOrderRow, order.id)
        assert row.provider == "tencent" and row.execution_provider == "eastmoney" and row.status == "processing"
    with pytest.raises(ValueError, match="state_conflict"):
        orders.prepare(order.id, execution_provider="sina")


def test_pending_orders_can_be_paginated_without_starving_other_accounts(setup):
    orders, sessions, service, instrument, _ = setup
    QuantAccounts(sessions).open("67890", Decimal("100000"))
    first, _ = orders.accept(service, command(), instrument, 100)
    other = QuantCommand(CommandSource(message_id="2", scope_kind="qq_group", target_id="999",
                                       sender_id="67890"), "buy", ("600000", "100"), 1,
                         1790000001, settings_version=1, settings=QuantSettings())
    second, _ = orders.accept(service, other, instrument, 100)
    page1 = orders.pending(limit=1)
    page2 = orders.pending(limit=1, after=(page1[0].created_at, page1[0].id))
    assert {page1[0].id, page2[0].id} == {first.id, second.id}


def test_fill_snapshot_and_ledger_are_committed_together_and_mismatch_rolls_back(setup):
    orders, sessions, service, instrument, account = setup
    first, _ = orders.accept(service, command(), instrument, 100)
    orders.start(first.id)
    fill = Fill("buy", 100, Decimal("10"), Decimal("1"), date(2026, 10, 26))
    occurred = datetime.fromisoformat("2026-10-26T09:31:00+08:00")
    with pytest.raises(ValueError, match="engine_mismatch"):
        orders.complete(
            first.id,
            fill,
            next_trading_day=date(2026, 10, 27),
            engine_cash=Decimal("99000"),
            engine_position=100,
            engine_state=b"engine-snapshot",
            engine_version="0.3.71",
            occurred_at=occurred,
        )
    with sessions() as session:
        assert session.get(QuantOrderRow, first.id).status == "processing"
        assert session.get(QuantAccountRow, account.id).cash == Decimal("100000")
        assert session.query(QuantFillRow).count() == 0
    result = orders.complete(
        first.id,
        fill,
        next_trading_day=date(2026, 10, 27),
        engine_cash=Decimal("98999"),
        engine_position=100,
        engine_state=b"engine-snapshot",
        engine_version="0.3.71",
        occurred_at=occurred,
    )
    assert result.status == "filled"
    with sessions() as session:
        assert session.get(QuantAccountRow, account.id).cash == Decimal("98999")
        assert session.get(QuantAccountRow, account.id).engine_state == b"engine-snapshot"
        assert session.query(QuantPositionRow).one().quantity == 100
        assert session.query(QuantLotRow).one().remaining_cost == Decimal("1001")
        assert session.query(QuantFillRow).count() == 1
        assert session.query(QuantFillRow).one().occurred_at.hour == 9
        assert session.query(QuantCashFlowRow).count() == 1
    history = orders.history("12345")
    assert history[0].quote_provider == "sina"
    assert len(history) == 1 and history[0].status == "filled" and history[0].filled_quantity == 100
    assert not orders.history("99999")
    with pytest.raises(ValueError, match="state_conflict"):
        orders.complete(
            first.id,
            fill,
            next_trading_day=date(2026, 10, 27),
            engine_cash=Decimal("98999"),
            engine_position=100,
            engine_state=b"engine-snapshot",
            engine_version="0.3.71",
            occurred_at=occurred,
        )
