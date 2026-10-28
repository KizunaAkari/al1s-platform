"""Normalize and validate market bars before feeding the strategy engine."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from al1s.quant.market_rules import CHINA


@dataclass(frozen=True, slots=True)
class MarketBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def normalize_auction_bars(bars: list[MarketBar]) -> list[MarketBar]:
    result = []
    for bar in bars:
        if bar.timestamp.tzinfo is None:
            raise ValueError("quant_bar_invalid")
        local_time = bar.timestamp.astimezone(CHINA).time().replace(tzinfo=None)
        if local_time == time(9, 30) or (
            local_time in {time(14, 58), time(14, 59)} and bar.volume == 0
        ):
            continue
        result.append(bar)
    return result


def validate_bars(
    bars: list[MarketBar], trading_days: set[date], frequency: str
) -> list[MarketBar]:
    if frequency not in {"daily", "1m"}:
        raise ValueError("quant_frequency_unsupported")
    if not trading_days or len(trading_days) > 8000 or len(bars) > 8000:
        raise ValueError("quant_data_range_invalid")
    dates: set[date] = set()
    stamps: set[datetime] = set()
    for bar in bars:
        if (
            not isinstance(bar.timestamp, datetime)
            or bar.timestamp.tzinfo is None
            or type(bar.volume) is not int
            or bar.volume <= 0
            or any(
                not isinstance(value, Decimal) or not value.is_finite() or value <= 0
                for value in (bar.open, bar.high, bar.low, bar.close)
            )
            or bar.high < max(bar.open, bar.close)
            or bar.low > min(bar.open, bar.close)
            or bar.high < bar.low
        ):
            raise ValueError("quant_bar_invalid")
        day = bar.timestamp.astimezone(CHINA).date()
        local = bar.timestamp.astimezone(CHINA)
        if (frequency == "daily" and day in dates) or local in stamps:
            raise ValueError("quant_bar_duplicate")
        if day not in trading_days:
            raise ValueError("quant_bar_nontrading_day")
        dates.add(day)
        stamps.add(local)
    if dates != trading_days:
        raise ValueError("quant_market_data_missing")
    if frequency == "1m":
        if len(trading_days) * 238 > 8000:
            raise ValueError("quant_data_range_invalid")
        expected = {
            datetime.combine(day, time(9, 31), tzinfo=CHINA) + timedelta(minutes=index)
            for day in trading_days
            for index in range(120)
        }
        expected |= {
            datetime.combine(day, time(13, 1), tzinfo=CHINA) + timedelta(minutes=index)
            for day in trading_days
            for index in range(117)
        }
        expected |= {datetime.combine(day, time(15), tzinfo=CHINA) for day in trading_days}
        auction_pairs = [
            {datetime.combine(day, time(14, minute), tzinfo=CHINA) for minute in (58, 59)}
            for day in trading_days
        ]
        optional = set().union(*auction_pairs)
        if (
            not expected <= stamps
            or stamps - expected - optional
            or any(stamps & pair and not pair <= stamps for pair in auction_pairs)
        ):
            raise ValueError("quant_market_data_missing")
    return sorted(bars, key=lambda item: item.timestamp)
