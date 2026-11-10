import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event, RLock, Thread
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.freqtrade.exit_notices import ExitNotices, exit_request_id, notice_text


def trade(**changes):
    return dict(
        trade_id=7,
        is_open=False,
        has_open_orders=False,
        exit_reason="liquidation",
        enter_tag="al1s-" + uuid4().hex,
        close_timestamp=1793203200000,
        pair="BTC/USDT:USDT",
        is_short=False,
        leverage=10,
        close_rate=60000,
        close_profit_abs=-80,
        funding_fees=-0.1,
        **changes,
    )


def test_notification_uses_native_exit_reason_and_original_entry_tag():
    item = trade()
    assert exit_request_id(item) is not None
    text = notice_text(item)
    assert "模拟强平" in text and "BTC" in text and "10" in text
    assert "-80" in text and "资金费用" in text
    item["exit_reason"] = "stop_loss"
    assert "模拟保护平仓" in notice_text(item)
    assert exit_request_id({"enter_tag": "force_enter"}) is None


def test_catchup_skips_pre_enable_history_and_only_advances_after_persisting():
    now = datetime.fromtimestamp(1793203200, UTC)
    profile = SimpleNamespace(
        id=uuid4(), market="futures", notices_since=now, exit_scan_at=now, qq_id="123"
    )
    recent = trade()
    old = {
        **trade(),
        "trade_id": 6,
        "close_timestamp": int((now - timedelta(seconds=1)).timestamp() * 1000),
    }
    native = MagicMock()
    native.call.return_value = {"trades": [recent, old]}
    repository = MagicMock()
    monitor = ExitNotices(repository)
    assert monitor.catch_up(profile, native)
    items = repository.enqueue_exits.call_args.args[1]
    assert items == [recent]
    assert repository.enqueue_exits.call_args.kwargs["checkpoint"] >= now


def test_partial_or_unconfirmed_native_exit_is_not_notified():
    repository, native = MagicMock(), MagicMock()
    monitor = ExitNotices(repository)
    profile = SimpleNamespace(id=uuid4())
    monitor.on_message(profile, native, {"type": "exit_fill", "is_final_exit": False})
    repository.enqueue_exits.assert_not_called()
    native.call.return_value = {**trade(), "is_open": True}
    try:
        monitor.on_message(
            profile, native, {"type": "exit_fill", "is_final_exit": True, "trade_id": 7}
        )
    except ValueError:
        pass
    repository.enqueue_exits.assert_not_called()


def test_catchup_pages_overlap_when_new_close_arrives_without_losing_older_trades():
    now = datetime.now(UTC)
    profile = SimpleNamespace(
        id=uuid4(),
        market="futures",
        qq_id="123",
        notices_since=now - timedelta(days=1),
        exit_scan_at=now - timedelta(days=1),
    )
    records = [
        {
            **trade(),
            "trade_id": i,
            "close_timestamp": int((now - timedelta(seconds=31 - i)).timestamp() * 1000),
        }
        for i in range(30, 0, -1)
    ]
    incoming = {**trade(), "trade_id": 31, "close_timestamp": int(now.timestamp() * 1000)}
    calls = []

    def page(path):
        calls.append(path)
        offset = int(path.split("offset=")[1].split("&")[0])
        if len(calls) == 2:
            records.insert(0, incoming)
        return {"trades": records[offset : offset + 20]}

    native, repository = MagicMock(), MagicMock()
    native.call.side_effect = page
    monitor = ExitNotices(repository)
    assert monitor.catch_up(profile, native)
    found = {
        row["trade_id"] for call in repository.enqueue_exits.call_args_list for row in call.args[1]
    }
    assert set(range(1, 31)) <= found
    monitor.catch_up(profile, native)
    assert any(
        row["trade_id"] == 31
        for call in repository.enqueue_exits.call_args_list
        for row in call.args[1]
    )


def test_observer_does_not_block_event_loop_while_cleanup_holds_manager_lock():
    manager = SimpleNamespace(lock=RLock(), instances={})
    held, release = Event(), Event()
    released = []

    def cleanup():
        with manager.lock:
            held.set()
            released.append(release.wait(1))

    thread = Thread(target=cleanup)
    thread.start()
    assert held.wait(1)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(ExitNotices(MagicMock()).run(manager, stop))
        await asyncio.sleep(0)
        release.set()
        stop.set()
        await task

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        thread.join()
    assert released == [True]
