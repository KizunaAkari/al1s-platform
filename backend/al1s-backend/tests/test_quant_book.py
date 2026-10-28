from datetime import date
from decimal import Decimal

import pytest

from al1s.quant.book import Fill, Lot, apply_fill


def test_buy_and_next_day_sell_with_fees_use_fifo_and_preserve_cash():
    buy = Fill("buy", 100, Decimal("10"), Decimal("1"), date(2026, 10, 26))
    bought = apply_fill(
        Decimal("100000"),
        [],
        buy,
        next_trading_day=date(2026, 10, 27),
        engine_cash=Decimal("98999"),
        engine_position=100,
    )
    assert bought.cash == Decimal("98999")
    assert bought.lots == (Lot(date(2026, 10, 26), date(2026, 10, 27), 100, Decimal("1001")),)
    sell = Fill("sell", 100, Decimal("11"), Decimal("1"), date(2026, 10, 27))
    sold = apply_fill(
        bought.cash,
        bought.lots,
        sell,
        next_trading_day=date(2026, 10, 28),
        engine_cash=Decimal("100098"),
        engine_position=0,
    )
    assert sold.cash == Decimal("100098") and not sold.lots


def test_t_plus_one_and_native_engine_mismatch_fail_closed():
    lots = [Lot(date(2026, 10, 26), date(2026, 10, 27), 100, Decimal("1000"))]
    same_day = Fill("sell", 100, Decimal("10"), Decimal("0"), date(2026, 10, 26))
    with pytest.raises(ValueError, match="available"):
        apply_fill(
            Decimal("99000"),
            lots,
            same_day,
            next_trading_day=date(2026, 10, 27),
            engine_cash=Decimal("100000"),
            engine_position=0,
        )
    buy = Fill("buy", 100, Decimal("10"), Decimal("0"), date(2026, 10, 26))
    with pytest.raises(ValueError, match="engine_mismatch"):
        apply_fill(
            Decimal("100000"),
            [],
            buy,
            next_trading_day=date(2026, 10, 27),
            engine_cash=Decimal("99000"),
            engine_position=99,
        )


def test_partial_sale_preserves_exact_lot_cost_after_rounding():
    lots = [Lot(date(2026, 10, 26), date(2026, 10, 27), 201, Decimal("1000"))]
    sell = Fill("sell", 100, Decimal("10"), Decimal("0"), date(2026, 10, 27))
    result = apply_fill(
        Decimal("0"),
        lots,
        sell,
        next_trading_day=date(2026, 10, 28),
        engine_cash=Decimal("1000"),
        engine_position=101,
    )
    assert result.lots[0].quantity == 101 and result.lots[0].cost == Decimal("502.4876")


def test_fill_precision_must_fit_persisted_fields():
    fill = Fill("buy", 100, Decimal("10.0000001"), Decimal("0"), date(2026, 10, 26))
    with pytest.raises(ValueError, match="precision"):
        apply_fill(
            Decimal("100000"),
            [],
            fill,
            next_trading_day=date(2026, 10, 27),
            engine_cash=Decimal("99000"),
            engine_position=100,
        )
