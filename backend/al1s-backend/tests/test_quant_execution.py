from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.adapters.postgres.quant_orders import PaperSnapshot, PendingOrder
from al1s.quant.execution import PaperExecution
from al1s.quant.market_data import MarketBar
from al1s.quant.paper_engine import PaperInstrument, PaperResult


def test_executor_waits_for_later_bars_and_commits_native_result():
    now = datetime.fromisoformat("2026-10-27T10:05:30+08:00")
    requested = now - timedelta(minutes=4)
    bars = [MarketBar(requested + timedelta(seconds=30 + i * 60), Decimal("10"), Decimal("11"),
                      Decimal("9"), Decimal("10"), 10000) for i in range(4)]
    order = PendingOrder(uuid4(), uuid4(), uuid4(), "SH", "600000", "main", "buy", 100,
                         int(requested.timestamp()), "accepted", Decimal(0), Decimal(0), Decimal(0),
                         Decimal(0), "sina")
    orders, source, cache, native = (MagicMock() for _ in range(4))
    orders.pending.return_value = [order]
    orders.prepare.return_value = PaperSnapshot(order, Decimal("100000"), Decimal("100000"), None,
                                                {"600000": 0}, (PaperInstrument("600000", "main"),))
    source.minutes.return_value = bars
    source.trading_days.side_effect = [{now.date()}, {now.date() + timedelta(days=1)}]
    native.return_value = PaperResult(Decimal("10"), Decimal("0"), 100, bars[2].timestamp,
                                      Decimal("99000"), {"600000": 100}, b"snapshot")
    executor = PaperExecution(orders, source, cache, native=native, now=lambda: now)
    executor.run_once()
    assert orders.prepare.call_count == 1 and orders.complete.call_count == 1
    assert all(bar.timestamp > requested for bar in native.call_args.args[1])


def test_engine_failure_marks_order_unknown_without_replay():
    now = datetime.fromisoformat("2026-10-27T10:05:30+08:00")
    requested = now - timedelta(minutes=4)
    bars = [MarketBar(requested + timedelta(seconds=30 + i * 60), Decimal("10"), Decimal("11"),
                      Decimal("9"), Decimal("10"), 10000) for i in range(4)]
    order = PendingOrder(uuid4(), uuid4(), uuid4(), "SH", "600000", "main", "buy", 100,
                         int(requested.timestamp()), "accepted", Decimal(0), Decimal(0), Decimal(0), Decimal(0), "sina")
    orders, source, cache, native = (MagicMock() for _ in range(4))
    orders.pending.return_value = [order]
    orders.prepare.return_value = PaperSnapshot(order, Decimal("100000"), Decimal("100000"), None,
                                                {"600000": 0}, (PaperInstrument("600000", "main"),))
    source.minutes.return_value = bars
    source.trading_days.side_effect = [{now.date()}, {now.date() + timedelta(days=1)}]
    native.side_effect = ValueError("result unknown")
    PaperExecution(orders, source, cache, native=native, now=lambda: now).run_once()
    orders.mark_unknown.assert_called_once_with(order.id)


def test_changed_corporate_action_expires_pending_order_before_native_execution():
    orders, source, cache, native = (MagicMock() for _ in range(4))
    orders.prepare.side_effect = ValueError("quant_corporate_needs_review")
    order_id = uuid4()
    now = datetime.fromisoformat("2026-10-27T10:05:30+08:00")
    requested = now - timedelta(minutes=4)
    bars = [MarketBar(requested + timedelta(minutes=i), Decimal("10"), Decimal("11"),
                      Decimal("9"), Decimal("10"), 100) for i in range(1, 4)]

    PaperExecution(orders, source, cache, native=native)._execute(order_id, bars, requested, now.date())

    orders.expire.assert_called_once_with(order_id)
    native.assert_not_called()


def test_zero_volume_bar_does_not_mark_pending_order_unknown():
    orders, source, cache, native = (MagicMock() for _ in range(4))
    now = datetime.fromisoformat("2026-10-27T10:05:00+08:00")
    requested = now - timedelta(minutes=4)
    bars = [MarketBar(requested + timedelta(minutes=i), Decimal("10"), Decimal("11"),
                      Decimal("9"), Decimal("10"), 0 if i == 2 else 100)
            for i in range(1, 4)]
    order_id = uuid4()

    PaperExecution(orders, source, cache, native=native)._execute(order_id, bars, requested, now.date())

    orders.prepare.assert_not_called()
    orders.mark_unknown.assert_not_called()
    native.assert_not_called()


def test_accepted_order_switches_provider_before_processing_without_replay():
    now = datetime.fromisoformat("2026-10-27T10:05:30+08:00")
    requested = now - timedelta(minutes=4)
    bars = [MarketBar(requested + timedelta(minutes=i), Decimal("10"), Decimal("11"),
                      Decimal("9"), Decimal("10"), 10000) for i in range(1, 4)]
    order = PendingOrder(uuid4(), uuid4(), uuid4(), "SH", "600000", "main", "buy", 100,
                         int(requested.timestamp()), "accepted", Decimal(0), Decimal(0), Decimal(0), Decimal(0), "sina")
    orders, source, cache, native = (MagicMock() for _ in range(4))
    source.configured.return_value = set()
    orders.pending.return_value = [order]
    source.minutes.side_effect = [ValueError("quant_source_unavailable"), bars]
    source.trading_days.side_effect = [{now.date()}, {now.date() + timedelta(days=1)}]
    orders.prepare.return_value = PaperSnapshot(order, Decimal("100000"), Decimal("100000"), None,
                                                {"600000": 0}, (PaperInstrument("600000", "main"),))
    native.return_value = PaperResult(Decimal("10"), Decimal("0"), 100, bars[-1].timestamp,
                                      Decimal("99000"), {"600000": 100}, b"snapshot")
    PaperExecution(orders, source, cache, native=native, now=lambda: now).run_once()
    assert [call.kwargs["provider"] for call in source.minutes.call_args_list] == ["sina", "tencent"]
    assert orders.prepare.call_args.kwargs["execution_provider"] == "tencent"
    assert cache.store.call_args.args[1] == "tencent"
    orders.complete.assert_called_once()


def test_executor_uses_closed_bars_when_source_includes_future_partial_minute():
    now = datetime.fromisoformat("2026-10-28T10:29:40+08:00")
    requested = now - timedelta(minutes=4)
    bars = [MarketBar(requested + timedelta(minutes=i), Decimal("10"), Decimal("11"),
                      Decimal("9"), Decimal("10"), 10000) for i in range(1, 4)]
    bars.append(MarketBar(now + timedelta(seconds=20), Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), 0))
    order = PendingOrder(uuid4(), uuid4(), uuid4(), "SH", "600000", "main", "buy", 100,
                         int(requested.timestamp()), "accepted", Decimal(0), Decimal(0), Decimal(0), Decimal(0), "sina")
    orders, source, cache, native = (MagicMock() for _ in range(4))
    source.configured.return_value = set()
    orders.pending.return_value = [order]
    source.minutes.return_value = bars
    source.trading_days.side_effect = [{now.date()}, {now.date() + timedelta(days=1)}]
    orders.prepare.return_value = PaperSnapshot(order, Decimal("100000"), Decimal("100000"), None,
                                                {"600000": 0}, (PaperInstrument("600000", "main"),))
    native.return_value = PaperResult(Decimal("10"), Decimal("0"), 100, bars[-2].timestamp,
                                      Decimal("99000"), {"600000": 100}, b"snapshot")
    PaperExecution(orders, source, cache, native=native, now=lambda: now).run_once()
    orders.complete.assert_called_once()
    assert all(bar.timestamp <= now for bar in native.call_args.args[1])
