from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from al1s.quant.contracts import QuantSettings


def test_provider_priority_contains_five_unique_sources_and_primary_first():
    from pydantic import ValidationError
    import pytest

    settings = QuantSettings()
    assert settings.provider_order == ["sina", "tencent", "eastmoney", "tushare", "gugudata"]
    with pytest.raises(ValidationError):
        QuantSettings(provider_order=["sina", "sina", "eastmoney", "tushare", "gugudata"])
    with pytest.raises(ValidationError):
        QuantSettings(provider="eastmoney", provider_order=["sina", "tencent", "eastmoney", "tushare", "gugudata"])
from al1s.quant.market_rules import check_order_quantity, check_quote


def test_settings_defaults_and_scope_validation():
    value = QuantSettings()
    assert value.initial_cash == Decimal("100000") and value.reply_interval_seconds == 10
    assert not value.enabled and value.max_quote_age_seconds == 120
    with pytest.raises(ValidationError):
        QuantSettings(groups=["123456", "123456"])
    with pytest.raises(ValidationError):
        QuantSettings(commission_rate=Decimal("-0.01"))
    with pytest.raises(ValidationError):
        QuantSettings(enabled=True)
    assert QuantSettings(event_ws_url="ws://llbot:3001").event_ws_url == "ws://llbot:3001"
    with pytest.raises(ValidationError):
        QuantSettings(event_ws_url="ws://user:password@llbot:3001")


def test_board_specific_order_lots_and_odd_lot_sales():
    assert check_order_quantity("main", "buy", 200, 0) == 200
    with pytest.raises(ValueError, match="quantity"):
        check_order_quantity("main", "buy", 201, 0)
    assert check_order_quantity("star", "buy", 201, 0) == 201
    with pytest.raises(ValueError, match="quantity"):
        check_order_quantity("star", "buy", 100, 0)
    assert check_order_quantity("beijing", "buy", 101, 0) == 101
    assert check_order_quantity("main", "sell", 199, 299) == 199
    with pytest.raises(ValueError, match="quantity"):
        check_order_quantity("main", "sell", 101, 299)
    assert check_order_quantity("star", "sell", 99, 99) == 99


def test_quote_must_be_recent_and_in_the_actual_trading_session():
    local = datetime.fromisoformat("2026-10-27T10:15:00+08:00")
    quote = local - timedelta(seconds=60)
    assert check_quote(Decimal("10.23"), quote, local, {date(2026, 10, 27)}, 120) == Decimal(
        "10.23"
    )
    with pytest.raises(ValueError, match="stale"):
        check_quote(Decimal("10"), local - timedelta(seconds=121), local, {local.date()}, 120)
    with pytest.raises(ValueError, match="session"):
        check_quote(Decimal("10"), quote, local, set(), 120)
    with pytest.raises(ValueError, match="stale"):
        check_quote(
            Decimal("10"), local + timedelta(seconds=1), local.astimezone(UTC), {local.date()}, 120
        )
    auction = datetime.fromisoformat("2026-10-27T14:58:00+08:00")
    with pytest.raises(ValueError, match="session"):
        check_quote(Decimal("10"), auction, auction, {auction.date()}, 120)
