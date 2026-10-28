from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from al1s.quant.market_data import MarketBar, normalize_auction_bars, validate_bars


def bar(day, price="10"):
    return MarketBar(datetime.fromisoformat(f"{day}T15:00:00+08:00"), Decimal(price),
                     Decimal("11"), Decimal("9"), Decimal("10"), 10000)


def test_daily_bars_require_complete_unique_trading_dates_and_valid_ohlc():
    days = {date(2026, 10, 26), date(2026, 10, 27)}
    result = validate_bars([bar("2026-10-27"), bar("2026-10-26")], days, "daily")
    assert [item.timestamp.date() for item in result] == sorted(days)
    with pytest.raises(ValueError, match="missing"):
        validate_bars([bar("2026-10-26")], days, "daily")
    with pytest.raises(ValueError, match="duplicate"):
        validate_bars([bar("2026-10-26"), bar("2026-10-26")], days, "daily")
    with pytest.raises(ValueError, match="bar_invalid"):
        validate_bars([bar("2026-10-26", price="12"), bar("2026-10-27")], days, "daily")


def test_minute_backtest_rejects_incomplete_intraday_data():
    day = date(2026, 10, 27)
    morning = datetime.fromisoformat("2026-10-27T09:31:00+08:00")
    afternoon = datetime.fromisoformat("2026-10-27T13:01:00+08:00")
    times = [morning + timedelta(minutes=i) for i in range(120)] + [afternoon + timedelta(minutes=i) for i in range(117)] + [datetime.fromisoformat("2026-10-27T15:00:00+08:00")]
    bars = [MarketBar(moment, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), 10000) for moment in times]
    assert len(validate_bars(bars, {day}, "1m")) == 238
    with pytest.raises(ValueError, match="missing"):
        validate_bars(bars[:-1], {day}, "1m")


def test_minute_backtest_accepts_complete_auction_minutes_when_source_provides_them():
    day = date(2026, 10, 27)
    morning = datetime.fromisoformat("2026-10-27T09:31:00+08:00")
    afternoon = datetime.fromisoformat("2026-10-27T13:01:00+08:00")
    times = [morning + timedelta(minutes=i) for i in range(120)] + [afternoon + timedelta(minutes=i) for i in range(120)]
    bars = [MarketBar(moment, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), 10000) for moment in times]
    assert len(validate_bars(bars, {day}, "1m")) == 240
    with pytest.raises(ValueError, match="missing"):
        validate_bars([bar for bar in bars if bar.timestamp.minute != 59 or bar.timestamp.hour != 14], {day}, "1m")


def test_no_trade_auction_minutes_are_excluded_without_filling_missing_continuous_bars():
    day = date(2026, 10, 27)
    morning = datetime.fromisoformat("2026-10-27T09:31:00+08:00")
    afternoon = datetime.fromisoformat("2026-10-27T13:01:00+08:00")
    times = ([datetime.fromisoformat("2026-10-27T09:30:00+08:00")]
             + [morning + timedelta(minutes=i) for i in range(120)]
             + [afternoon + timedelta(minutes=i) for i in range(120)])
    bars = [MarketBar(t, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"),
                      0 if t.hour == 14 and t.minute in (58, 59) else 100) for t in times]
    normalized = normalize_auction_bars(bars)
    assert len(validate_bars(normalized, {day}, "1m")) == 238
    normalized[1] = MarketBar(normalized[1].timestamp, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), 0)
    with pytest.raises(ValueError, match="quant_bar_invalid"):
        validate_bars(normalized, {day}, "1m")
