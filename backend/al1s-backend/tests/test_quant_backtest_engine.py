from datetime import date, datetime
from dataclasses import replace
from decimal import Decimal

import pytest

from al1s.quant.backtest_engine import BacktestRequest, validate_backtest
from al1s.quant.market_data import MarketBar


def test_only_supported_strategies_and_complete_market_data_are_accepted():
    bar = MarketBar(datetime.fromisoformat("2026-10-27T15:00:00+08:00"), Decimal("10"),
                    Decimal("11"), Decimal("9"), Decimal("10"), 10000)
    data = BacktestRequest(symbol="600000", board="main", strategy="buy_hold", bars=[bar],
                           trading_days={date(2026, 10, 27)}, initial_cash=Decimal("100000"),
                           commission_rate=Decimal(0), minimum_commission=Decimal(0),
                           stamp_tax_rate=Decimal(0), slippage=Decimal(0), fast=5, slow=20)
    assert validate_backtest(data) == [bar]
    with pytest.raises(ValueError, match="strategy"):
        validate_backtest(replace(data, strategy="import os"))
