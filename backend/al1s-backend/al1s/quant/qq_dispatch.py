"""Dispatch QQ commands and send persisted replies through one global gate."""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import structlog

from al1s.adapters.postgres.meme_settings import QqConnection
from al1s.adapters.postgres.quant_replies import QuantReplies
from al1s.meme_workshop.onebot import OneBotClient
from al1s.quant.commands import QuantCommand
from al1s.quant.process_client import QuantProcessClient


class QuantQqDispatcher:
    def __init__(self, process: QuantProcessClient, replies: QuantReplies) -> None:
        self.process, self.replies = process, replies
        self._stop = asyncio.Event()
        self.log = structlog.get_logger()

    async def dispatch(
        self, row: QqConnection, client: OneBotClient, command: QuantCommand
    ) -> None:
        if command.settings is None:
            raise ValueError("quant_snapshot_missing")
        try:
            content = await asyncio.to_thread(self.process.execute, row.service_id, command)
        except Exception:
            content = (
                "请求结果未确认，请使用模拟成交查询，避免重复下单。"
                if command.action in {"buy", "sell"}
                else "量化服务暂时不可用，请稍后重试。"
            )
        await asyncio.to_thread(
            self.replies.enqueue,
            row.service_id,
            command,
            content,
            command.settings.reply_interval_seconds,
        )

    async def send_once(self, clients: dict[UUID, OneBotClient], now: datetime) -> bool:
        reply = await asyncio.to_thread(
            self.replies.claim_next, now, eligible_services=set(clients)
        )
        if reply is None:
            return False
        status = "unknown"
        try:
            await asyncio.to_thread(clients[reply.service_id].send, reply.source, reply.content)
            status = "sent"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.warning(
                "quant_qq_reply_unknown", service_id=str(reply.service_id), error=type(exc).__name__
            )
        finally:
            await asyncio.shield(
                asyncio.to_thread(self.replies.settle, reply.id, status, datetime.now(UTC))
            )
        return True

    async def run(self, clients: Callable[[], dict[UUID, OneBotClient]]) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.replies.recover_sending, datetime.now(UTC))
                break
            except Exception as exc:
                self.log.warning("quant_qq_recovery_unavailable", error=type(exc).__name__)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=5)
        while not self._stop.is_set():
            try:
                await self.send_once(clients(), datetime.now(UTC))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.warning("quant_qq_send_unavailable", error=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=1)

    def stop(self) -> None:
        self._stop.set()
