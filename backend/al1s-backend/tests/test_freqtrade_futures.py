from decimal import Decimal

import pytest

from al1s.freqtrade.commands import parse_futures_order
from al1s.freqtrade.contracts import FreqtradeSettings
from al1s.freqtrade.native_config import native_config


def test_futures_margin_leverage_and_close_units_are_explicit():
    settings = FreqtradeSettings(futures_enabled=True, futures_max_leverage=20)
    assert parse_futures_order("long", ("BTC", "100", "10"), settings) == (
        "BTC/USDT:USDT",
        Decimal("100"),
        10,
    )
    assert parse_futures_order("short", ("ETH", "50"), settings) == (
        "ETH/USDT:USDT",
        Decimal("50"),
        3,
    )
    assert parse_futures_order("close", ("SOL", "全部"), settings) == ("SOL/USDT:USDT", None, None)
    for args in [("BTC", "100", "21"), ("BTC", "100", "0"), ("BTC", "100", "1.5")]:
        with pytest.raises(ValueError):
            parse_futures_order("long", args, settings)
    with pytest.raises(ValueError):
        FreqtradeSettings(futures_default_leverage=10, futures_max_leverage=5)


def test_futures_native_config_is_isolated_paper_and_keeps_spot_default():
    settings = FreqtradeSettings(futures_enabled=True)
    kwargs = dict(
        initial_cash=Decimal("1000"),
        db_url="postgresql+psycopg://test@db/ft",
        directory="/data/futures",
        port=19001,
        password="local-secret",
    )
    future = native_config(settings, market="futures", **kwargs)
    spot = native_config(settings, **kwargs)
    assert future["dry_run"] is True and future["trading_mode"] == "futures"
    assert future["margin_mode"] == "isolated" and future["fee"] == 0.0005
    assert future["exchange"]["pair_whitelist"] == [
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "SOL/USDT:USDT",
    ]
    assert future["strategy"] == "FuturesManualPaperStrategy"
    assert spot["trading_mode"] == "spot" and spot["fee"] == 0.001
