from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.adapters.postgres.quant_instruments import Instrument
from al1s.adapters.postgres.quant_orders import Order
from al1s.meme_workshop.contracts import CommandSource
from al1s.quant.commands import QuantCommand
from al1s.quant.contracts import QuantSettings
from al1s.quant.market_data import MarketBar
from al1s.quant.trade import QuantTrade


def test_submit_uses_current_quote_sender_and_t_plus_one():
    now = datetime.fromisoformat("2026-10-27T10:15:30+08:00")
    bar = MarketBar(datetime.fromisoformat("2026-10-27T10:15:00+08:00"), Decimal("10"),
                    Decimal("11"), Decimal("9"), Decimal("10"), 10000)
    source, accounts, instruments, orders, cache = (MagicMock() for _ in range(5))
    source.trading_days.return_value = {now.date()}
    source.minutes.return_value = [bar]
    account = MagicMock(state="active")
    accounts.get.return_value = account
    instrument = Instrument(uuid4(), "SH", "600000", "main")
    instruments.register.return_value = instrument
    order = Order(uuid4(), "accepted", "buy", 100)
    orders.accept.return_value = (order, True)
    corporate = MagicMock()
    handler = QuantTrade(source, accounts, instruments, orders, cache, corporate=corporate, now=lambda: now)
    source_event = CommandSource(message_id="1", scope_kind="qq_group", target_id="123", sender_id="456")
    command = QuantCommand(source_event, "buy", ("600000", "100"), 1, int(now.timestamp()),
                           settings=QuantSettings(), settings_version=1)
    assert "已受理" in handler.submit(uuid4(), command)
    accounts.get.assert_called_with("456")
    assert orders.accept.call_args.args[2:] == (instrument.id, 100)
    corporate.ensure.assert_called_once_with(instrument, account)
    sell = QuantCommand(source_event, "sell", ("600000", "100"), 1, int(now.timestamp()),
                        settings=QuantSettings(), settings_version=1)
    accounts.available_for.return_value = 0
    with pytest.raises(ValueError, match="available"):
        handler.submit(uuid4(), sell)
    empty = MarketBar(bar.timestamp, bar.open, bar.high, bar.low, bar.close, 0)
    source.minutes.return_value = [empty]
    with pytest.raises(ValueError, match="source_unavailable"):
        handler.submit(uuid4(), command)


def test_submit_falls_back_and_records_actual_quote_provider():
    now = datetime.fromisoformat("2026-10-27T10:15:30+08:00")
    bar = MarketBar(datetime.fromisoformat("2026-10-27T10:15:00+08:00"), Decimal("10"),
                    Decimal("11"), Decimal("9"), Decimal("10"), 10000)
    source, accounts, instruments, orders, cache = (MagicMock() for _ in range(5))
    source.configured.return_value = set()
    source.trading_days.return_value = {now.date()}
    source.minutes.side_effect = [ValueError("quant_source_unavailable"), [bar]]
    accounts.get.return_value = MagicMock(state="active")
    instrument = Instrument(uuid4(), "SH", "600000", "main")
    instruments.register.return_value = instrument
    orders.accept.return_value = (Order(uuid4(), "accepted", "buy", 100), True)
    command = QuantCommand(CommandSource(message_id="2", scope_kind="qq_group", target_id="123", sender_id="456"),
                           "buy", ("600000", "100"), 1, int(now.timestamp()), settings=QuantSettings(), settings_version=1)
    result = QuantTrade(source, accounts, instruments, orders, cache, corporate=MagicMock(), now=lambda: now).submit(uuid4(), command)
    assert "腾讯" in result
    assert [call.kwargs["provider"] for call in source.minutes.call_args_list] == ["sina", "tencent"]
    assert orders.accept.call_args.kwargs["quote_provider"] == "tencent"
    assert cache.store.call_args.args[1] == "tencent"


def test_replayed_order_reply_uses_original_quote_provider_and_price():
    now = datetime.fromisoformat("2026-10-27T10:15:30+08:00")
    bar = MarketBar(datetime.fromisoformat("2026-10-27T10:15:00+08:00"), Decimal("12"),
                    Decimal("13"), Decimal("11"), Decimal("12"), 100)
    source, accounts, instruments, orders, cache = (MagicMock() for _ in range(5))
    source.configured.return_value = set()
    source.trading_days.return_value = {now.date()}
    source.minutes.return_value = [bar]
    accounts.get.return_value = MagicMock(state="active")
    instruments.register.return_value = Instrument(uuid4(), "SH", "600000", "main")
    orders.accept.return_value = (Order(uuid4(), "accepted", "buy", 100, "tencent", bar.timestamp, Decimal("10")), False)
    command = QuantCommand(CommandSource(message_id="2", scope_kind="qq_group", target_id="123", sender_id="456"),
                           "buy", ("600000", "100"), 1, int(now.timestamp()), settings=QuantSettings(), settings_version=1)
    result = QuantTrade(source, accounts, instruments, orders, cache, corporate=MagicMock(), now=lambda: now).submit(uuid4(), command)
    assert "腾讯" in result and "10 元" in result and "12 元" not in result


def test_invalid_latest_ohlc_falls_back_before_accepting_order():
    now = datetime.fromisoformat("2026-10-27T10:15:30+08:00")
    stamp = datetime.fromisoformat("2026-10-27T10:15:00+08:00")
    bad = MarketBar(stamp, Decimal("10"), Decimal("9"), Decimal("8"), Decimal("10"), 100)
    good = MarketBar(stamp, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), 100)
    source, accounts, instruments, orders, cache = (MagicMock() for _ in range(5))
    source.configured.return_value = set()
    source.trading_days.return_value = {now.date()}
    source.minutes.side_effect = [[bad], [good]]
    accounts.get.return_value = MagicMock(state="active")
    instruments.register.return_value = Instrument(uuid4(), "SH", "600000", "main")
    orders.accept.return_value = (Order(uuid4(), "accepted", "buy", 100), True)
    command = QuantCommand(CommandSource(message_id="3", scope_kind="qq_group", target_id="123", sender_id="456"),
                           "buy", ("600000", "100"), 1, int(now.timestamp()), settings=QuantSettings(), settings_version=1)
    result = QuantTrade(source, accounts, instruments, orders, cache, corporate=MagicMock(), now=lambda: now).submit(uuid4(), command)
    assert "腾讯" in result and orders.accept.call_args.kwargs["quote_provider"] == "tencent"


def test_incomplete_future_minute_is_not_used_as_current_quote():
    now = datetime.fromisoformat("2026-10-28T10:29:40+08:00")
    closed = MarketBar(datetime.fromisoformat("2026-10-28T10:29:00+08:00"), Decimal("10"),
                       Decimal("11"), Decimal("9"), Decimal("10"), 100)
    pending = MarketBar(datetime.fromisoformat("2026-10-28T10:30:00+08:00"), Decimal("10"),
                        Decimal("11"), Decimal("9"), Decimal("10"), 0)
    source, accounts, instruments, orders, cache = (MagicMock() for _ in range(5))
    source.configured.return_value = set()
    source.trading_days.return_value = {now.date()}
    source.minutes.return_value = [closed, pending]
    accounts.get.return_value = MagicMock(state="active")
    instruments.register.return_value = Instrument(uuid4(), "SH", "600000", "main")
    orders.accept.return_value = (Order(uuid4(), "accepted", "buy", 100), True)
    command = QuantCommand(CommandSource(message_id="4", scope_kind="qq_group", target_id="123", sender_id="456"),
                           "buy", ("600000", "100"), 1, int(now.timestamp()), settings=QuantSettings(), settings_version=1)
    result = QuantTrade(source, accounts, instruments, orders, cache, corporate=MagicMock(), now=lambda: now).submit(uuid4(), command)
    assert "新浪" in result and orders.accept.call_args.kwargs["quote_time"] == closed.timestamp
