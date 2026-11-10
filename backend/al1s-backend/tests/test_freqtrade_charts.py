from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from al1s.freqtrade.charts import fetch_chart, parse_chart
from al1s.freqtrade.commands import validate_words
from al1s.freqtrade.contracts import DEFAULT_COMMANDS, FUTURES_COMMANDS, SPOT_COMMANDS


def candles(now, minutes=60):
    end = int(now.timestamp() // 60 * 60000)
    return [
        [
            end - (minutes - i) * 60000,
            "100",
            "102",
            "99",
            str(100 + i),
            "1",
            end - (minutes - i - 1) * 60000 - 1,
        ]
        for i in range(minutes)
    ]


def test_chart_defaults_and_legacy_mapping_sets_remain_supported():
    assert parse_chart(("btc",))[1] == "1小时"
    assert parse_chart(("SOL/USDT:USDT", "7天"))[0] == "SOL"
    assert len(DEFAULT_COMMANDS) == 19
    for words in (SPOT_COMMANDS, {**SPOT_COMMANDS, **FUTURES_COMMANDS}, DEFAULT_COMMANDS):
        validate_words(words, set())
    with pytest.raises(ValueError):
        parse_chart(("DOGE",))
    with pytest.raises(ValueError):
        parse_chart(("BTC", "一年"))


def test_chart_uses_fixed_public_market_and_only_complete_bars():
    now = datetime(2026, 10, 29, 0, 0, 20, tzinfo=UTC)
    client = MagicMock()
    client.fapiPublicGetKlines.return_value = candles(now)
    result = fetch_chart(client, ("BTC",), "futures", now=now)
    assert len(result["prices"]) == 60 and result["symbol"] == "BTC"
    assert result["market"] == "futures"
    params = client.fapiPublicGetKlines.call_args.args[0]
    assert params["symbol"] == "BTCUSDT" and params["interval"] == "1m"
    assert params["endTime"] < int(now.timestamp() * 1000) and params["limit"] == 180
    client.publicGetKlines.assert_not_called()


@pytest.mark.parametrize("change", ["gap", "stale", "nan"])
def test_chart_rejects_missing_stale_or_invalid_public_data(change):
    now = datetime(2026, 10, 29, 0, 0, 20, tzinfo=UTC)
    rows = candles(now)
    if change == "gap":
        rows.pop(10)
    elif change == "stale":
        rows.pop()
    else:
        rows[10][4] = "NaN"
    client = MagicMock()
    client.publicGetKlines.return_value = rows
    with pytest.raises(ValueError, match="行情"):
        fetch_chart(client, ("BTC",), "spot", now=now)
