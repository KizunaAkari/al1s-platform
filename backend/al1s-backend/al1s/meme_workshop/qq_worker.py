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
from al1s.adapters.postgres.quant_settings import QuantSettingsRepository
from al1s.meme_workshop.contracts import RenderedImage, RenderInputs
from al1s.meme_workshop.copied_mentions import CopiedMentionError, resolve_copied_targets
from al1s.meme_workshop.images import decode_image
from al1s.meme_workshop.media_fetch import read_media
from al1s.meme_workshop.onebot import OneBotClient, QqMessage, parse_event
from al1s.meme_workshop.records import TemplateRecord
from al1s.meme_workshop.service import MemeWorkshopService
from al1s.quant.commands import QuantCommand
from al1s.quant.gateway import QuantGateway
from al1s.quant.process_client import QuantProcessClient
from al1s.quant.qq_dispatch import QuantQqDispatcher
from al1s.secrets.ports import SecretCipher


def read_qq_avatar(user_id: str) -> bytes:
    body = read_media(f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640")
    if decode_image(body).size == (40, 40):
        return read_media(f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100")
    return body


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
        quant_repository: QuantSettingsRepository | None = None,
        quant_gateway: QuantGateway | None = None,
        quant_process: QuantProcessClient | None = None,
        quant_dispatch: Callable[[QqConnection, OneBotClient, QuantCommand], Awaitable[None]]
        | None = None,
        quant_sender: QuantQqDispatcher | None = None,
    ) -> None:
        self.workshop, self.cipher = workshop, cipher
        self._connections = connections
        self._quant_repository, self._quant_gateway = quant_repository, quant_gateway
        self._quant_process, self._quant_dispatch = quant_process, quant_dispatch
        self._quant_sender = quant_sender
        self._quant_sender_task: asyncio.Task[None] | None = None
        self._clients: dict[UUID, OneBotClient] = {}
        self._quant_ids: set[UUID] = set()
        self._meme_ids: set[UUID] = set()
        self._quant_tasks: set[asyncio.Task[None]] = set()
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

    def is_quant_connected(self, service_id: UUID, event_url: str | None) -> bool:
        return bool(event_url and service_id in self._quant_ids and service_id in self._sockets
                    and service_id in self._tasks
                    and self._tasks[service_id][0].event_url == event_url
                    and self._quant_gateway
                    and self._quant_gateway.matches_connection(service_id, event_url))

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
            self._meme_ids = {row.service_id for row in rows}
            desired = {row.service_id: row for row in rows}
            quant_ids: set[UUID] = set()
            if self._quant_repository and self._quant_gateway and self._quant_process:
                try:
                    quant_rows = await asyncio.to_thread(self._quant_repository.qq_connections)
                    snapshots = await asyncio.to_thread(
                        self._quant_repository.snapshots, [row.service_id for row in quant_rows]
                    )
                    for row in quant_rows:
                        record = snapshots.get(row.service_id)
                        existing = desired.get(row.service_id)
                        if record is None or (existing and (existing.event_url != row.event_url
                                                           or existing.base_url != row.base_url)):
                            continue
                        applied = await asyncio.to_thread(
                            self._quant_process.activate, row.service_id, record)
                        if applied == (record.row_version, record.mapping_version):
                            self._quant_gateway.activate(row.service_id, record)
                        elif (self._quant_gateway.applied_versions(row.service_id) != applied
                              or not self._quant_gateway.matches_connection(
                                  row.service_id, row.event_url)):
                            continue
                        desired.setdefault(row.service_id, row)
                        quant_ids.add(row.service_id)
                except Exception as exc:
                    self.log.warning("quant_qq_configuration_unavailable", error=type(exc).__name__)
            self._quant_ids = quant_ids
            templates = await asyncio.to_thread(
                self.workshop.repository.list_templates, limit=500, enabled_only=True
            )
            if self._stopped:
                return
            self._keywords = {
                word for template in templates for word in [template.keyword, *template.aliases]
            }
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
        if self._quant_sender:
            self._quant_sender_task = asyncio.create_task(
                self._quant_sender.run(self.quant_clients))
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
        if self._quant_sender:
            self._quant_sender.stop()
        tasks = [task for _, task in self._tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        quant_tasks = list(self._quant_tasks)
        for task in quant_tasks:
            task.cancel()
        if quant_tasks:
            await asyncio.gather(*quant_tasks, return_exceptions=True)
        self._quant_tasks.clear()
        if self._quant_sender_task:
            await asyncio.gather(self._quant_sender_task, return_exceptions=True)
            self._quant_sender_task = None
        self._tasks.clear()
        sockets = list(self._sockets.values())
        self._sockets.clear()
        if sockets:
            await asyncio.gather(*(socket.close() for socket in sockets), return_exceptions=True)
        self._clients.clear()

    def quant_clients(self) -> dict[UUID, OneBotClient]:
        return {identity: client for identity, client in self._clients.items()
                if identity in self._tasks and
                self.is_quant_connected(identity, self._tasks[identity][0].event_url)}

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
                    self._clients[row.service_id] = client
                    async for body in socket:
                        if self._stopped:
                            return
                        try:
                            await self.handle_event(row, client, json.loads(body))
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
                self._clients.pop(row.service_id, None)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=5)

    async def handle_event(self, row: QqConnection, client: OneBotClient, event: object) -> None:
        message = parse_event(event, self._keywords)
        if message and row.service_id in self._meme_ids:
            await self.handle(row, client, message)
            return
        if (row.service_id not in self._quant_ids or not self._quant_gateway
                or not self._quant_repository or not self._quant_dispatch):
            return
        if not self._quant_gateway.matches_connection(row.service_id, row.event_url):
            return
        command = self._quant_gateway.dispatch(row.service_id, event, set())
        if command is None or await asyncio.to_thread(
            self._quant_repository.keyword_conflicts, command.normalized_keyword
        ):
            return
        while len(self._quant_tasks) >= 128 and not self._stopped:
            await asyncio.wait(self._quant_tasks, return_when=asyncio.FIRST_COMPLETED)
        if self._stopped:
            return
        task = asyncio.create_task(self._run_quant(row, client, command))
        self._quant_tasks.add(task)
        task.add_done_callback(self._quant_tasks.discard)

    async def _run_quant(
        self, row: QqConnection, client: OneBotClient, command: QuantCommand
    ) -> None:
        try:
            if self._quant_dispatch:
                await self._quant_dispatch(row, client, command)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.warning("quant_qq_request_failed", service_id=str(row.service_id),
                             error=type(exc).__name__)

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
        except CopiedMentionError as exc:
            error, result = exc.code, exc.reply_text
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
        selected, avatar_ids, texts = await self.message_targets(
            row.service_id, receipt, client, message
        )
        if self._stopped:
            raise ValueError("meme_stopped")
        urls = message.image_urls
        if not urls and message.reply_id:
            urls = await asyncio.to_thread(client.reply_images, message.reply_id)
        if len(urls) > 1 or len(avatar_ids) > 4:
            raise ValueError("meme_input_count")
        avatars, images = await asyncio.gather(
            asyncio.gather(
                *(
                    asyncio.to_thread(read_qq_avatar, identity)
                    for identity in avatar_ids
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
            RenderInputs(list(avatars), list(images), texts), selected_template=selected,
        )

    async def message_targets(
        self, bot_id: UUID, receipt: UUID, client: OneBotClient, message: QqMessage
    ) -> tuple[TemplateRecord | None, list[str], list[str]]:
        if not message.copied_target_text:
            return None, message.avatar_ids, message.texts
        selected: TemplateRecord | None = None
        if message.keyword != "随机表情":
            selected = await asyncio.to_thread(
                self.workshop.repository.find_keyword, message.keyword
            )
            if selected is None:
                raise ValueError("template_not_found")
            if self._stopped:
                raise ValueError("meme_stopped")
            if not await asyncio.to_thread(
                self.workshop.repository.bind_template, bot_id, receipt, selected.version_id
            ):
                raise ValueError("receipt_unavailable")
            if self._stopped:
                raise ValueError("meme_stopped")
            if not await asyncio.to_thread(
                self.workshop.repository.lease_version, selected.version_id
            ):
                raise ValueError("template_not_found")
            if self._stopped:
                raise ValueError("meme_stopped")
        if selected is not None and not selected.layout.requirements()[0]:
            return selected, [], message.texts
        targets = await self.copied_targets(client, message)
        return selected, targets, message.texts[1:]

    async def copied_targets(self, client: OneBotClient, message: QqMessage) -> list[str]:
        if self._stopped:
            raise ValueError("meme_stopped")
        if message.source.scope_kind != "qq_group":
            raise CopiedMentionError("copied_targets_group_only")
        if not message.copied_target_text or len(message.copied_target_text) > 200:
            raise CopiedMentionError("copied_targets_invalid")
        try:
            members = await asyncio.to_thread(client.group_members, message.source.target_id)
        except (ValueError, OSError) as exc:
            raise CopiedMentionError("copied_members_unavailable") from exc
        if self._stopped:
            raise ValueError("meme_stopped")
        return resolve_copied_targets(message.copied_target_text, members, message.source.sender_id)
