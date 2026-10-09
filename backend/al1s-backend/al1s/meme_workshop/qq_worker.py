import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

import structlog
from websockets.asyncio.client import ClientConnection, connect

from al1s.adapters.postgres.meme_settings import QqConnection
from al1s.meme_workshop.contracts import RenderedImage, RenderInputs
from al1s.meme_workshop.media_fetch import read_media
from al1s.meme_workshop.onebot import OneBotClient, QqMessage, parse_event
from al1s.meme_workshop.service import MemeWorkshopService
from al1s.secrets.ports import SecretCipher


class DirectConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc  # Credentials stay at the configured gateway; redirects are never followed.


class QqWorker:
    def __init__(
        self,
        workshop: MemeWorkshopService,
        cipher: SecretCipher,
        *,
        connections: Callable[[], Awaitable[list[QqConnection]]] | None = None,
    ) -> None:
        self.workshop, self.cipher = workshop, cipher
        self._connections = connections
        self._stopped = False
        self._refreshing = False
        self._stop = asyncio.Event()
        self._tasks: dict[UUID, tuple[QqConnection, asyncio.Task[None]]] = {}
        self._sockets: dict[UUID, ClientConnection] = {}
        self._keywords: set[str] = set()
        self._slots = asyncio.Semaphore(2)
        self._cooldown: dict[str, float] = {}
        self.log = structlog.get_logger()

    @property
    def connection_count(self) -> int:
        return len(self._tasks)

    async def refresh(self) -> None:
        if self._stopped or self._refreshing:
            return
        self._refreshing = True
        try:
            rows = (
                await self._connections()
                if self._connections is not None
                else await asyncio.to_thread(self.workshop.repository.qq_connections)
            )
            if self._stopped:
                return
            templates = await asyncio.to_thread(
                self.workshop.repository.list_templates, limit=500, enabled_only=True
            )
            if self._stopped:
                return
            self._keywords = {
                word for template in templates for word in [template.keyword, *template.aliases]
            }
            desired = {row.service_id: row for row in rows}
            for identity, (row, task) in list(self._tasks.items()):
                if desired.get(identity) != row or task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    self._tasks.pop(identity, None)
            for identity, row in desired.items():
                if self._stopped:
                    break
                if identity not in self._tasks:
                    self._tasks[identity] = (row, asyncio.create_task(self.listen(row)))
        finally:
            self._refreshing = False

    async def run(self) -> None:
        while not self._stopped:
            try:
                await self.refresh()
                now = datetime.now(UTC)
                await asyncio.to_thread(self.workshop.repository.cleanup, now)
                await asyncio.to_thread(self.workshop.repository.cleanup_assets, now)
            except Exception as exc:
                self.log.warning("meme_qq_configuration_unavailable", error=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=30)

    async def stop(self) -> None:
        self._stopped = True
        self._stop.set()
        tasks = [task for _, task in self._tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        sockets = list(self._sockets.values())
        self._sockets.clear()
        if sockets:
            await asyncio.gather(*(socket.close() for socket in sockets), return_exceptions=True)

    async def listen(self, row: QqConnection) -> None:
        try:
            token = (
                self.cipher.decrypt(row.secret_ciphertext, row.secret_key_id)
                if row.secret_ciphertext and row.secret_key_id
                else None
            )
            client = OneBotClient(row.base_url, token)
        except Exception:
            self.log.warning("meme_qq_connection_invalid", service_id=str(row.service_id))
            return
        while not self._stopped:
            try:
                async with DirectConnect(
                    row.event_url,
                    proxy=None,
                    open_timeout=5,
                    close_timeout=2,
                    max_size=131072,
                    max_queue=4,
                    additional_headers=({"Authorization": f"Bearer {token}"} if token else None),
                ) as socket:
                    if self._stopped:
                        return
                    self._sockets[row.service_id] = socket
                    async for body in socket:
                        if self._stopped:
                            return
                        try:
                            message = parse_event(json.loads(body), self._keywords)
                            if message:
                                await self.handle(row, client, message)
                        except (ValueError, TypeError):
                            self.log.warning(
                                "meme_qq_event_rejected", service_id=str(row.service_id)
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.warning(
                    "meme_qq_event_connection_unavailable", service_id=str(row.service_id)
                )
            finally:
                self._sockets.pop(row.service_id, None)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=5)

    async def handle(self, row: QqConnection, client: OneBotClient, message: QqMessage) -> None:
        key = f"{row.service_id}:{message.source.target_id}:{message.source.sender_id}"
        if self._stopped or self._cooldown.get(key, 0) > time.monotonic():
            return
        receipt = await asyncio.to_thread(
            self.workshop.repository.claim, row.service_id, message.source
        )
        if receipt is None or self._stopped:
            return
        self._cooldown[key] = time.monotonic() + row.cooldown_seconds
        while len(self._cooldown) > 4096:
            self._cooldown.pop(next(iter(self._cooldown)))
        error: str | None = None
        try:
            async with asyncio.timeout(12):
                if self._slots.locked():
                    raise ValueError("render_busy")
                async with self._slots:
                    result: RenderedImage | str = await self.generate(row, client, message, receipt)
        except (TimeoutError, ValueError, OSError):
            error = "meme_input_or_render_failed"
            result = "无法生成表情, 请检查头像、单张图片和文字。发送“表情列表”查看用法。"
        if self._stopped:
            return
        if not await asyncio.to_thread(
            self.workshop.repository.start_send, row.service_id, receipt, error_code=error
        ):
            return
        status = "unknown"
        try:
            await asyncio.to_thread(client.send, message.source, result)
            status = "sent"
        except Exception:
            self.log.warning("meme_qq_reply_result_unknown", service_id=str(row.service_id))
        await asyncio.to_thread(
            self.workshop.repository.settle, row.service_id, receipt, status, error
        )

    async def generate(
        self, row: QqConnection, client: OneBotClient, message: QqMessage, receipt: UUID
    ) -> RenderedImage | str:
        if message.keyword == "表情列表":
            templates = await asyncio.to_thread(
                self.workshop.repository.list_templates, limit=15, enabled_only=True
            )
            return "可用表情: \n" + "\n".join(f"{item.keyword} · {item.name}" for item in templates)
        urls = message.image_urls
        if not urls and message.reply_id:
            urls = await asyncio.to_thread(client.reply_images, message.reply_id)
        if len(urls) > 1 or len(message.avatar_ids) > 4:
            raise ValueError("meme_input_count")
        avatars, images = await asyncio.gather(
            asyncio.gather(
                *(
                    asyncio.to_thread(read_media, f"https://q1.qlogo.cn/g?b=qq&nk={identity}&s=640")
                    for identity in message.avatar_ids
                )
            ),
            asyncio.gather(*(asyncio.to_thread(read_media, url) for url in urls)),
        )
        if self._stopped:
            raise ValueError("meme_stopped")
        return await asyncio.to_thread(
            self.workshop.render_command,
            row.service_id,
            receipt,
            message.keyword,
            RenderInputs(list(avatars), list(images), message.texts),
        )
