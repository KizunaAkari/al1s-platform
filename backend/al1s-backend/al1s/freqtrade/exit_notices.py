import asyncio
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

from websockets.asyncio.client import connect

BEIJING = timezone(timedelta(hours=8))


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def exit_request_id(trade: dict) -> UUID | None:
    tag = trade.get("enter_tag", "")
    if isinstance(tag, str) and tag.startswith("al1s-") and len(tag) == 37:
        with suppress(ValueError):
            return UUID(hex=tag[5:])
    return None


def close_time(trade: dict) -> datetime:
    return datetime.fromtimestamp(trade["close_timestamp"] / 1000, UTC)


def final_exit(trade: dict) -> bool:
    return (
        trade.get("is_open") is False
        and not trade.get("has_open_orders")
        and isinstance(trade.get("trade_id"), int)
        and isinstance(trade.get("close_timestamp"), (int, float))
    )


def notice_text(trade: dict) -> str:
    reason = {"liquidation": "模拟强平", "stop_loss": "模拟保护平仓"}[trade["exit_reason"]]
    direction = "空" if trade.get("is_short") else "多"

    def shown(key):
        value = trade.get(key)
        return "不可用" if value is None else f"{float(value):.8g}"

    return (
        f"{reason}｜{trade['pair']} {direction} {shown('leverage')}倍\n"
        f"退出价 {shown('close_rate')} USDT\n"
        f"原生已实现盈亏 {shown('close_profit_abs')} USDT\n"
        f"累计资金费用 {shown('funding_fees')} USDT\n"
        f"成交时间 {close_time(trade).astimezone(BEIJING):%Y-%m-%d %H:%M:%S} 北京时间\n"
        "Freqtrade模拟结果，费用及盈亏按原生口径展示。"
    )


class ExitNotices:
    def __init__(self, repository):
        self.repository = repository
        self.pages = {}
        self.log = logging.getLogger(__name__)

    def catch_up(self, profile, native) -> bool:
        since = max(utc(profile.notices_since), utc(profile.exit_scan_at) - timedelta(seconds=60))
        offset, started = self.pages.get(profile.id, (0, datetime.now(UTC)))
        for _ in range(10):
            items = native.call(f"trades?limit=20&offset={offset}&order_by_id=false")["trades"]
            valid = [item for item in items if final_exit(item) and close_time(item) >= since]
            done = len(items) < 20 or any(
                final_exit(item) and close_time(item) < since for item in items
            )
            self.repository.enqueue_exits(profile, valid, checkpoint=started if done else None)
            if done:
                profile.exit_scan_at = started
                self.pages.pop(profile.id, None)
                return True
            offset += len(items)
        self.pages[profile.id] = (offset, started)
        return False

    def on_message(self, profile, native, message):
        if message.get("type") != "exit_fill" or message.get("is_final_exit") is not True:
            return
        trade_id = message.get("trade_id")
        if type(trade_id) is not int or trade_id <= 0:
            raise ValueError("freqtrade_exit_identity_invalid")
        trade = native.call(f"trade/{trade_id}")
        if not final_exit(trade):
            raise ValueError("freqtrade_exit_not_committed")
        self.repository.enqueue_exits(profile, [trade], checkpoint=close_time(trade))
        profile.exit_scan_at = max(utc(profile.exit_scan_at), close_time(trade))

    async def watch(self, profile, native, stop):
        while not stop.is_set() and native.process.poll() is None:
            try:
                async with connect(
                    f"ws://127.0.0.1:{native.port}/api/v1/message/ws?token={native.password}",
                    proxy=None,
                    open_timeout=5,
                    max_size=65536,
                ) as socket:
                    await socket.send(json.dumps({"type": "subscribe", "data": ["exit_fill"]}))
                    while not await asyncio.to_thread(self._catch_up_locked, profile, native):
                        await asyncio.sleep(0.1)
                    while not stop.is_set() and native.process.poll() is None:
                        try:
                            message = await asyncio.wait_for(socket.recv(), timeout=1)
                        except TimeoutError:
                            continue
                        await asyncio.to_thread(
                            self._message_locked, profile, native, json.loads(message)
                        )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.log.warning(
                    "freqtrade_exit_observer_retry profile=%s error=%s",
                    profile.id,
                    type(error).__name__,
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=5)

    def _catch_up_locked(self, profile, native):
        with native.notice_lock:
            return self.catch_up(profile, native)

    def _message_locked(self, profile, native, message):
        with native.notice_lock:
            self.on_message(profile, native, message)

    async def run(self, manager, stop):
        tasks = {}
        try:
            while not stop.is_set():
                active = await asyncio.to_thread(self._active, manager)
                for key, (instance, task) in list(tasks.items()):
                    if active.get(key) is not instance or task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        tasks.pop(key)
                for key, instance in active.items():
                    if key not in tasks:
                        tasks[key] = (
                            instance,
                            asyncio.create_task(self.watch(instance.profile, instance, stop)),
                        )
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1)
        finally:
            for _, task in tasks.values():
                task.cancel()
            await asyncio.gather(*(task for _, task in tasks.values()), return_exceptions=True)

    @staticmethod
    def _active(manager):
        with manager.lock:
            return {
                key: value
                for key, value in manager.instances.items()
                if value.profile is not None
                and value.profile.market == "futures"
                and value.process.poll() is None
            }
