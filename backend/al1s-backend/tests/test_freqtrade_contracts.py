from decimal import Decimal

import pytest

from al1s.freqtrade.commands import parse_order
from al1s.freqtrade.contracts import FreqtradeSettings
from al1s.freqtrade.native_config import native_config


def test_buy_uses_usdt_and_sell_uses_coin_quantity():
    assert parse_order("buy", ("btc", "100")) == ("BTC/USDT", Decimal("100"))
    assert parse_order("sell", ("ETH/USDT", "0.001")) == ("ETH/USDT", Decimal("0.001"))
    assert parse_order("sell", ("SOL", "全部")) == ("SOL/USDT", None)
    for args in [("DOGE", "10"), ("BTC", "nan"), ("BTC", "-1"), ("BTC", "0")]:
        with pytest.raises(ValueError):
            parse_order("buy", args)


def test_native_configuration_enforces_paper_spot_and_isolated_endpoint():
    config = native_config(
        FreqtradeSettings(),
        initial_cash=Decimal("1000"),
        db_url="postgresql+psycopg://test@db/ft?options=-csearch_path%3Dft_test",
        directory="/data/account",
        port=19001,
        password="local-secret",
    )
    assert config["dry_run"] is True and config["trading_mode"] == "spot"
    assert config["exchange"]["enable_ws"] is False
    assert config["exchange"]["key"] == config["exchange"]["secret"] == ""
    assert config["api_server"]["listen_ip_address"] == "127.0.0.1"
    assert config["api_server"]["listen_port"] == 19001
    assert config["dry_run_wallet"] == 1000
    assert config["minimal_roi"] == {} and config["stoploss"] == -1


def test_enabled_settings_require_a_connection_and_bound_capacity():
    with pytest.raises(ValueError):
        FreqtradeSettings(enabled=True)
    with pytest.raises(ValueError):
        FreqtradeSettings(max_active_accounts=0)
