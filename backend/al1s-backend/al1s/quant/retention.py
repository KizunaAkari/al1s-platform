"""Trim minute cache only after obtaining a verified trading-day cutoff."""

from datetime import UTC, datetime, time, timedelta
from threading import Event

from al1s.adapters.postgres.quant_market_data import QuantMarketData
from al1s.adapters.postgres.quant_settings import QuantSettingsRepository
from al1s.quant.market_rules import CHINA
from al1s.quant.source_client import MarketSource


class QuantRetention:
    def __init__(
        self,
        settings: QuantSettingsRepository,
        cache: QuantMarketData,
        source: MarketSource,
        *,
        now=lambda: datetime.now(UTC),
    ):
        self.settings, self.cache, self.source, self.now = settings, cache, source, now
        self._stop = Event()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> int:
        days = self.settings.cache_days()
        if not 1 <= days <= 365:
            raise ValueError("quant_cache_range_invalid")
        today = self.now().astimezone(CHINA).date()
        start = today - timedelta(days=days * 3 + 30)
        available = sorted(
            self.source.trading_days(start.strftime("%Y%m%d"), today.strftime("%Y%m%d"))
        )
        if len(available) < days:
            raise ValueError("quant_calendar_coverage_missing")
        cutoff = datetime.combine(available[-days], time(0), tzinfo=CHINA)
        removed = 0
        for _ in range(50):
            if self._stop.is_set():
                break
            count = self.cache.cleanup_minutes(cutoff, limit=10000)
            removed += count
            if count < 10000:
                break
        return removed
