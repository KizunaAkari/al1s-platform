from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.base import Base
from al1s.adapters.postgres.quant_accounts import QuantAccounts
from al1s.adapters.postgres.quant_models import (
    QuantAccountRow,
    QuantInstrumentRow,
    QuantLotRow,
    QuantQuoteRow,
    QuantPositionRow,
)


@pytest.fixture
def accounts():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            QuantAccountRow.__table__,
            QuantInstrumentRow.__table__,
            QuantPositionRow.__table__,
            QuantLotRow.__table__,
            QuantQuoteRow.__table__,
        ],
    )
    return QuantAccounts(sessionmaker(engine, expire_on_commit=False))


def test_open_is_idempotent_and_never_resets_existing_balance(accounts):
    first, created = accounts.open("12345", Decimal("100000"))
    assert created and first.cash == Decimal("100000")
    again, created = accounts.open("12345", Decimal("200000"))
    assert not created and again.id == first.id and again.cash == Decimal("100000")
    other, created = accounts.open("67890", Decimal("200000"))
    assert created and other.id != first.id


def test_positions_are_scoped_to_requesting_account_and_paginated(accounts):
    first, _ = accounts.open("12345", Decimal("100000"))
    other, _ = accounts.open("67890", Decimal("100000"))
    with accounts.sessions.begin() as session:
        stocks = [
            QuantInstrumentRow(market="SH", current_code=code, board="main", name=code, active=True)
            for code in ("600000", "600001", "600002")
        ]
        session.add_all(stocks)
        session.flush()
        for account_id, stock in [
            (first.id, stocks[0]),
            (first.id, stocks[1]),
            (other.id, stocks[2]),
        ]:
            session.add(
                QuantPositionRow(
                    account_id=account_id,
                    instrument_id=stock.id,
                    quantity=100,
                    total_cost=Decimal("1000"),
                    updated_at=datetime.now(UTC),
                )
            )
    page = accounts.positions("12345", limit=1, offset=0)
    assert len(page) == 1 and page[0].current_code in {"600000", "600001"}
    assert {p.current_code for p in accounts.positions("12345", limit=10, offset=0)} == {
        "600000",
        "600001",
    }
    assert [p.current_code for p in accounts.positions("67890", limit=10, offset=0)] == ["600002"]
    with pytest.raises(ValueError):
        accounts.positions("12345", limit=101, offset=0)


def test_available_quantity_is_calculated_per_lot_without_changing_ownership(accounts):
    account, _ = accounts.open("12345", Decimal("100000"))
    with accounts.sessions.begin() as session:
        stock = QuantInstrumentRow(market="SH", current_code="600000", board="main", name="name", active=True)
        session.add(stock); session.flush()
        position = QuantPositionRow(account_id=account.id, instrument_id=stock.id, quantity=200,
                                    total_cost=Decimal("2000"), updated_at=datetime.now(UTC))
        session.add(position); session.flush()
        session.add_all([QuantLotRow(position_id=position.id, acquired_on=date(2026, 10, 26),
                                     available_on=date(2026, 10, 27), remaining_quantity=100,
                                     remaining_cost=Decimal("1000")),
                         QuantLotRow(position_id=position.id, acquired_on=date(2026, 10, 27),
                                     available_on=date(2026, 10, 28), remaining_quantity=100,
                                     remaining_cost=Decimal("1000"))])
    row = accounts.positions("12345", as_of=date(2026, 10, 27))[0]
    assert row.quantity == 200 and row.available == 100
    assert accounts.available_for("12345", stock.id, date(2026, 10, 27)) == 100
    assert accounts.available_for("99999", stock.id, date(2026, 10, 27)) == 0


def test_asset_summary_reports_missing_prices_instead_of_fake_equity(accounts):
    account, _ = accounts.open("12345", Decimal("100000"))
    now = datetime.now(UTC)
    with accounts.sessions.begin() as session:
        stock = QuantInstrumentRow(market="SH", current_code="600000", board="main", name="name", active=True)
        session.add(stock); session.flush()
        session.add(QuantPositionRow(account_id=account.id, instrument_id=stock.id, quantity=100,
                                     total_cost=Decimal("1000"), updated_at=now))
    summary = accounts.summary("12345", provider="sina")
    assert summary.positions == 1 and summary.priced == 0 and summary.market_value is None
    for provider in ("tencent", "tushare", "gugudata"):
        assert accounts.summary("12345", provider=provider).market_value is None
    with accounts.sessions.begin() as session:
        session.add(QuantQuoteRow(instrument_id=stock.id, provider="sina", price=Decimal("11"),
                                  quote_time=now, fetched_at=now))
    summary = accounts.summary("12345", provider="sina")
    assert summary.priced == 1 and summary.market_value == Decimal("1100")
