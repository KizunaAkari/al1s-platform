from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.adapters.postgres.quant_backtests import BacktestRecord
from al1s.adapters.postgres.quant_instruments import Instrument
from al1s.meme_workshop.contracts import CommandSource
from al1s.quant.backtest_engine import BacktestResult
from al1s.quant.commands import QuantCommand
from al1s.quant.contracts import QuantSettings
from al1s.quant.market_data import MarketBar
from al1s.quant.backtest_service import QuantBacktest


def test_backtest_uses_complete_source_data_and_reports_its_rules():
    source, accounts, instruments, cache, repository, native = (MagicMock() for _ in range(6))
    source.trading_days.return_value = {date(2026, 10, 26), date(2026, 10, 27)}
    source.daily_sina.return_value = [MarketBar(datetime.fromisoformat(f"2026-10-{day}T15:00:00+08:00"),
        Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), 10000) for day in (26, 27)]
    accounts.get.return_value = MagicMock()
    instrument = Instrument(uuid4(), "SH", "600000", "main")
    instruments.register.return_value = instrument
    repository.begin.return_value = (BacktestRecord(uuid4(), "processing", None, None, None, None), True)
    repository.find.return_value = None
    native.return_value = BacktestResult(Decimal("0.1"), Decimal("0.05"), 1, Decimal("110000"), "a"*64)
    command = QuantCommand(CommandSource(message_id="1", scope_kind="qq_group", target_id="5", sender_id="123"),
        "backtest", ("买入持有", "600000", "2026-10-26", "2026-10-27"), 1, 1790000000,
        settings_version=1, settings=QuantSettings())
    result = QuantBacktest(source, accounts, instruments, cache, repository, native=native,
                           now=lambda: datetime.fromisoformat("2026-10-28T10:00:00+08:00")).submit(uuid4(), command)
    assert "新浪" in result and "75%" in result and "10.00%" in result
    repository.complete.assert_called_once()


def test_backtest_refetches_complete_interval_from_fallback_source():
    source, accounts, instruments, cache, repository, native = (MagicMock() for _ in range(6))
    source.configured.return_value = set()
    source.trading_days.return_value = {date(2026, 10, 26), date(2026, 10, 27)}
    source.daily_sina.return_value = [MarketBar(datetime.fromisoformat("2026-10-26T15:00:00+08:00"),
        Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), 10000)]
    source.daily_extra.return_value = [MarketBar(datetime.fromisoformat(f"2026-10-{day}T15:00:00+08:00"),
        Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), 10000) for day in (26, 27)]
    accounts.get.return_value = MagicMock()
    instruments.register.return_value = Instrument(uuid4(), "SH", "600000", "main")
    repository.begin.return_value = (BacktestRecord(uuid4(), "processing", None, None, None, None), True)
    repository.find.return_value = None
    native.return_value = BacktestResult(Decimal("0.1"), Decimal("0.05"), 1, Decimal("110000"), "a"*64)
    command = QuantCommand(CommandSource(message_id="2", scope_kind="qq_group", target_id="5", sender_id="123"),
        "backtest", ("买入持有", "600000", "2026-10-26", "2026-10-27"), 1, 1790000000,
        settings_version=1, settings=QuantSettings())
    result = QuantBacktest(source, accounts, instruments, cache, repository, native=native,
                           now=lambda: datetime.fromisoformat("2026-10-28T10:00:00+08:00")).submit(uuid4(), command)
    assert "腾讯" in result
    assert source.daily_extra.call_args.args[0] == "tencent"
    assert cache.store.call_args.args[1] == "tencent"
    assert repository.begin.call_args.args[6] == "tencent"
    assert len(native.call_args.args[0].bars) == 2


def test_completed_replay_returns_original_provider_without_fetching_again():
    source, accounts, instruments, cache, repository, native = (MagicMock() for _ in range(6))
    accounts.get.return_value = MagicMock()
    repository.find.return_value = BacktestRecord(uuid4(), "completed", Decimal("0.1"), Decimal("0.05"), 1, "a" * 64, "tencent")
    command = QuantCommand(CommandSource(message_id="2", scope_kind="qq_group", target_id="5", sender_id="123"),
        "backtest", ("买入持有", "600000", "2026-10-26", "2026-10-27"), 1, 1790000000,
        settings_version=1, settings=QuantSettings())
    result = QuantBacktest(source, accounts, instruments, cache, repository, native=native,
                           now=lambda: datetime.fromisoformat("2026-10-28T10:00:00+08:00")).submit(uuid4(), command)
    assert "腾讯" in result
    source.trading_days.assert_not_called()
    native.assert_not_called()
