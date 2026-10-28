from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from al1s.quant.retention import QuantRetention


def test_retention_uses_trading_days_and_keeps_data_when_calendar_is_missing():
    settings, cache, source = MagicMock(), MagicMock(), MagicMock()
    settings.cache_days.return_value = 2
    source.trading_days.return_value = {date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27)}
    cache.cleanup_minutes.return_value = 3
    now = datetime.fromisoformat("2026-10-27T16:00:00+08:00")
    retention = QuantRetention(settings, cache, source, now=lambda: now)
    assert retention.run_once() == 3
    assert cache.cleanup_minutes.call_args.args[0].date() == date(2026, 10, 26)
    source.trading_days.return_value = set()
    cache.reset_mock()
    with pytest.raises(ValueError, match="coverage"):
        retention.run_once()
    cache.cleanup_minutes.assert_not_called()
