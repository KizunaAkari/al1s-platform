import asyncio
import contextlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID
import unicodedata

import structlog
from websockets.asyncio.client import ClientConnection, connect

from al1s.adapters.postgres.meme_settings import QqConnection
from al1s.adapters.postgres.quant_settings import QuantSettingsRepository
from al1s.bot_ai.messages import is_sticker_collect
from al1s.meme_workshop.contracts import RenderedImage, RenderInputs
from al1s.meme_workshop.copied_mentions import CopiedMentionError, resolve_copied_targets
from al1s.meme_workshop.images import decode_image
from al1s.meme_workshop.media_fetch import read_media
from al1s.meme_workshop.onebot import OneBotClient, QqMessage, command_parts, identity, parse_event
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


@dataclass
class _ClaimLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


def _is_drawing_command(event: object) -> bool:
    if not isinstance(event, dict) or event.get("post_type") != "message":
        return False
    parts = event.get("message")
    if not isinstance(parts, list):
        return False
    body = "".join(part["data"].get("text", "") for part in parts
                   if isinstance(part, dict) and part.get("type") == "text"
                   and isinstance(part.get("data"), dict) and isinstance(part["data"].get("text"), str))
    body = unicodedata.normalize("NFKC", body).strip()
    return bool(re.match(r"^/(?:wf[0-9]{0,9}|gpt[0-9]{0,9}|p2p|tag)(?:\s|$)", body, re.IGNORECASE))


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
        freqtrade_adapter=None,
        ai_adapter=None,
        drawing_adapter=None,
    ) -> None:
        self.workshop, self.cipher = workshop, cipher
        self._connections = connections
        self._quant_repository, self._quant_gateway = quant_repository, quant_gateway
        self._quant_process, self._quant_dispatch = quant_process, quant_dispatch
        self._quant_sender = quant_sender
        self._freqtrade_adapter = freqtrade_adapter
        self._ai_adapter = ai_adapter
        self._drawing_adapter = drawing_adapter
        self._quant_sender_task: asyncio.Task[None] | None = None
        self._clients: dict[UUID, OneBotClient] = {}
        self._quant_ids: set[UUID] = set()
        self._meme_ids: set[UUID] = set()
        self._quant_tasks: set[asyncio.Task[None]] = set()
        self._event_tasks: dict[asyncio.Task[None], tuple[UUID, OneBotClient]] = {}
        self._ai_admission_tails: dict[tuple, asyncio.Future] = {}
        self._busy_tasks: dict[UUID, asyncio.Task] = {}
        self._claim_locks: dict[str, _ClaimLock] = {}
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

    def drawing_client(self, service_id: UUID) -> OneBotClient | None:
        return self._clients.get(service_id) if service_id in self._sockets and not self._stopped else None

    def drawing_connection_status(self, service_id: UUID, event_url: str | None) -> str:
        if not event_url:
            return "unconfigured"
        current = self._tasks.get(service_id)
        if current is not None and current[0].event_url != event_url:
            return "conflict"
        if current is not None and service_id in self._sockets and not self._stopped:
            return "connected"
        return "connecting"

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
            if self._freqtrade_adapter:
                try:
                    for row in await self._freqtrade_adapter.refresh():
                        existing = desired.get(row.service_id)
                        if existing is None or (existing.event_url == row.event_url
                                                 and existing.base_url == row.base_url):
                            desired.setdefault(row.service_id, row)
                except Exception as exc:
                    self.log.warning('freqtrade_configuration_unavailable', error=type(exc).__name__)
            if self._ai_adapter:
                try:
                    for row in await self._ai_adapter.refresh():
                        existing = desired.get(row.service_id)
                        if existing is None or (existing.event_url == row.event_url
                                                 and existing.base_url == row.base_url):
                            desired.setdefault(row.service_id, row)
                except Exception as exc:
                    self.log.warning("ai_qq_configuration_unavailable", error=type(exc).__name__)
            if self._drawing_adapter:
                try:
                    for row in await self._drawing_adapter.refresh():
                        existing = desired.get(row.service_id)
                        if existing is None or (existing.event_url == row.event_url
                                                 and existing.base_url == row.base_url):
                            desired.setdefault(row.service_id, row)
                        else:
                            self.log.warning("drawing_qq_connection_conflict", service_id=str(row.service_id))
                except Exception as exc:
                    self.log.warning("drawing_qq_configuration_unavailable", error=type(exc).__name__)
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
        event_tasks = list(self._event_tasks)
        for task in event_tasks:
            task.cancel()
        if event_tasks:
            await asyncio.gather(*event_tasks, return_exceptions=True)
        self._event_tasks.clear()
        busy_tasks = tuple(self._busy_tasks.values())
        for task in busy_tasks:
            task.cancel()
        if busy_tasks:
            await asyncio.gather(*busy_tasks, return_exceptions=True)
        self._busy_tasks.clear()
        quant_tasks = list(self._quant_tasks)
        for task in quant_tasks:
            task.cancel()
        if quant_tasks:
            await asyncio.gather(*quant_tasks, return_exceptions=True)
        self._quant_tasks.clear()
        if self._ai_adapter:
            await self._ai_adapter.stop()
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
                (self.is_quant_connected(identity, self._tasks[identity][0].event_url)
                 or self.is_freqtrade_connected(identity, self._tasks[identity][0].event_url))}

    def is_freqtrade_connected(self, service_id: UUID, event_url: str | None) -> bool:
        return bool(self._freqtrade_adapter and service_id in self._sockets
                    and service_id in self._tasks
                    and self._tasks[service_id][0].event_url == event_url
                    and self._freqtrade_adapter.matches_connection(service_id, event_url))

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
                            self.submit_event(row, client, json.loads(body))
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
                event_tasks = [task for task, owner in self._event_tasks.items()
                               if owner == (row.service_id, client)]
                for task in event_tasks:
                    task.cancel()
                if event_tasks:
                    await asyncio.gather(*event_tasks, return_exceptions=True)
                self._sockets.pop(row.service_id, None)
                self._clients.pop(row.service_id, None)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=5)

    def submit_event(self, row: QqConnection, client: OneBotClient, event: object) -> bool:
        if self._stopped:
            return False
        if len(self._event_tasks) >= 128:
            self.log.warning("qq_event_capacity_exhausted", service_id=str(row.service_id))
            if (self._ai_adapter and len(self._busy_tasks) < 32
                    and row.service_id not in self._busy_tasks
                    and not self._ai_reserved(row.service_id, event)):
                task = asyncio.create_task(self._ai_adapter.reject_busy(row, client, event))
                self._busy_tasks[row.service_id] = task
                task.add_done_callback(lambda done: self._busy_tasks.pop(row.service_id, None)
                                       if self._busy_tasks.get(row.service_id) is done else None)
            return False

        admission = self._reserve_ai_admission(row.service_id, event)

        async def process() -> None:
            try:
                await self.handle_event(row, client, event, admission=admission)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.warning("qq_event_processing_failed", service_id=str(row.service_id),
                                 error=type(exc).__name__)
            finally:
                if admission is not None:
                    admission[1]()

        task = asyncio.create_task(process())
        self._event_tasks[task] = (row.service_id, client)
        task.add_done_callback(lambda completed: self._event_tasks.pop(completed, None))
        return True

    def _reserve_ai_admission(self, service_id: UUID, event: object):
        if (not self._ai_adapter or not isinstance(event, dict) or event.get("post_type") != "message"
                or _is_drawing_command(event)):
            return None
        kind = event.get("message_type")
        target = identity(event.get("group_id") if kind == "group" else event.get("user_id"))
        if kind not in {"group", "private"} or target is None:
            return None
        key = (service_id, kind, target)
        previous = self._ai_admission_tails.get(key)
        completed = asyncio.get_running_loop().create_future()
        self._ai_admission_tails[key] = completed
        released = False

        def release():
            nonlocal released
            if released:
                return
            released = True

            def complete(_previous=None):
                if not completed.done():
                    completed.set_result(None)
                if self._ai_admission_tails.get(key) is completed:
                    self._ai_admission_tails.pop(key, None)

            if previous is not None and not previous.done():
                previous.add_done_callback(complete)
            else:
                complete()

        return previous, release

    async def handle_event(self, row: QqConnection, client: OneBotClient, event: object,
                           *, admission=None) -> None:
        if self._ai_adapter and is_sticker_collect(event):
            if admission is not None and admission[0] is not None:
                await asyncio.shield(admission[0])
            await self._ai_adapter.accept(row, client, event)
            return
        message = parse_event(event, self._keywords)
        if message:
            if admission is not None:
                admission[1]()
            if row.service_id in self._meme_ids:
                await self.handle(row, client, message)
            return
        if self._freqtrade_adapter:
            command = await self._freqtrade_adapter.parse(row, event)
            if command is not None:
                if admission is not None:
                    admission[1]()
                if len(self._quant_tasks) >= 128:
                    await asyncio.to_thread(self._freqtrade_adapter.replies.enqueue,
                        row.service_id, command, "机器人繁忙，请稍后重试。",
                        command.settings.reply_interval_seconds)
                    return
                if not self._stopped:
                    task = asyncio.create_task(self._freqtrade_adapter.execute(row, command))
                    self._quant_tasks.add(task)
                    task.add_done_callback(self._quant_tasks.discard)
                return
        if (row.service_id in self._quant_ids and self._quant_gateway
                and self._quant_repository and self._quant_dispatch
                and self._quant_gateway.matches_connection(row.service_id, row.event_url)):
            command = self._quant_gateway.dispatch(row.service_id, event, set())
            if command is not None:
                if admission is not None:
                    admission[1]()
                if await asyncio.to_thread(
                    self._quant_repository.keyword_conflicts, command.normalized_keyword
                ):
                    return
                if len(self._quant_tasks) >= 128:
                    if self._quant_sender and command.settings:
                        await asyncio.to_thread(self._quant_sender.replies.enqueue,
                            row.service_id, command, "机器人繁忙，请稍后重试。",
                            command.settings.reply_interval_seconds)
                    return
                if not self._stopped:
                    task = asyncio.create_task(self._run_quant(row, client, command))
                    self._quant_tasks.add(task)
                    task.add_done_callback(self._quant_tasks.discard)
                return
        if _is_drawing_command(event):
            if self._drawing_adapter and (not self._ai_adapter or not self._ai_reserved(row.service_id, event)):
                await self._drawing_adapter.accept(row, client, event)
            return
        if self._ai_adapter and not self._ai_reserved(row.service_id, event):
            if admission is not None and admission[0] is not None:
                await asyncio.shield(admission[0])
            await self._ai_adapter.accept(row, client, event)

    def _ai_reserved(self, service_id: UUID, event: object) -> bool:
        if not isinstance(event, dict) or not isinstance(event.get("message"), list):
            return False
        words = self._keywords | self._ai_adapter.reserved.get(service_id, set())
        body = "".join(segment.get("data", {}).get("text", "")
            for segment in event["message"] if isinstance(segment, dict)
            and segment.get("type") == "text" and isinstance(segment.get("data"), dict)
            and isinstance(segment["data"].get("text"), str))
        parts = command_parts(body, words)
        return bool(parts and unicodedata.normalize("NFKC", parts[0]).casefold() in words)

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

    async def send_ai_meme(self, item, template_key: str, target: str) -> dict:
        service_id = item.service_id
        connection = self._tasks.get(service_id)
        client = self._clients.get(service_id)
        if (not self._ai_adapter or service_id not in self._meme_ids or not connection
                or service_id not in self._sockets or client is None):
            return {"status": "denied", "code": "meme_unavailable"}
        row = connection[0]
        if target != "self":
            if item.source.scope_kind != "qq_group" or target not in item.mentions:
                return {"status": "denied", "code": "ai_meme_target_denied"}
            try:
                members = await asyncio.wait_for(asyncio.to_thread(
                    client.group_members, item.source.target_id), timeout=6)
            except (TimeoutError, OSError, ValueError):
                return {"status": "failed", "code": "meme_members_unavailable"}
            if not any(identity(member.get("user_id")) == target for member in members):
                return {"status": "denied", "code": "ai_meme_target_denied"}
        avatar = item.source.sender_id if target == "self" else target
        keyword = unicodedata.normalize("NFKC", template_key).strip().casefold()
        template = await asyncio.to_thread(self.workshop.repository.find_keyword, keyword)
        if template is None:
            return {"status": "failed", "code": "meme_template_unavailable"}
        avatars, images, texts = template.layout.requirements()
        if avatars > 1 or images or texts:
            return {"status": "failed", "code": "meme_inputs_required"}

        async def permitted() -> bool:
            current = self._tasks.get(service_id)
            return bool(not self._stopped and current and current[0] == row
                and self._clients.get(service_id) is client and service_id in self._sockets
                and self._ai_adapter.hub.is_active(item.request_id)
                and await asyncio.to_thread(self._ai_adapter.repository.allowed, service_id,
                                            item.source, item.config_version)
                and await asyncio.to_thread(self.workshop.repository.allowed, service_id, item.source)
                and self._ai_adapter.hub.is_active(item.request_id))

        if not await permitted():
            return {"status": "denied", "code": "ai_scope_revoked"}
        message = QqMessage(item.source, keyword, [avatar] if avatars else [], [], [], None)
        return await self.handle(row, client, message, can_send=permitted,
                                 can_start=lambda: self._ai_adapter.hub.is_active(item.request_id))

    async def handle(self, row: QqConnection, client: OneBotClient, message: QqMessage, *,
                     can_send: Callable[[], Awaitable[bool]] | None = None,
                     can_start: Callable[[], bool] | None = None) -> dict:
        key = f"{row.service_id}:{message.source.target_id}:{message.source.sender_id}"
        gate = self._claim_locks.setdefault(key, _ClaimLock())
        gate.users += 1
        try:
            async with gate.lock:
                if self._stopped or self._cooldown.get(key, 0) > time.monotonic():
                    return {"status": "rate_limited", "code": "meme_cooldown"}
                receipt = await asyncio.to_thread(
                    self.workshop.repository.claim, row.service_id, message.source
                )
                if receipt is None or self._stopped:
                    return {"status": "failed", "code": "meme_claim_unavailable"}
                self._cooldown[key] = time.monotonic() + row.cooldown_seconds
                while len(self._cooldown) > 4096:
                    self._cooldown.pop(next(iter(self._cooldown)))
        finally:
            gate.users -= 1
            if gate.users == 0:
                self._claim_locks.pop(key, None)
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
            return {"status": "failed", "code": "meme_stopped"}
        if can_send is not None and not await can_send():
            await asyncio.to_thread(self.workshop.repository.settle, row.service_id,
                                    receipt, "failed", "ai_scope_revoked")
            return {"status": "denied", "code": "ai_scope_revoked"}
        if not await asyncio.to_thread(
            self.workshop.repository.start_send, row.service_id, receipt, error_code=error
        ):
            return {"status": "failed", "code": "meme_send_unavailable"}
        if can_send is not None and not await can_send():
            await asyncio.to_thread(self.workshop.repository.settle, row.service_id,
                                    receipt, "failed", "ai_scope_revoked")
            return {"status": "denied", "code": "ai_scope_revoked"}
        status, sent_message_id = "unknown", None
        try:
            def send_if_active():
                if can_start is not None and not can_start():
                    return None
                return client.send(message.source, result)
            sent_message_id = await asyncio.to_thread(send_if_active)
            status = "failed" if sent_message_id is None and can_start is not None else "sent"
            if status == "failed":
                error = "ai_scope_revoked"
        except Exception:
            self.log.warning("meme_qq_reply_result_unknown", service_id=str(row.service_id))
        finally:
            await asyncio.shield(asyncio.to_thread(
                self.workshop.repository.settle, row.service_id, receipt, status, error
            ))
        if status == "unknown":
            return {"status": "unknown", "code": "meme_send_unconfirmed"}
        if status == "failed":
            return {"status": "denied", "code": "ai_scope_revoked"}
        if error is not None:
            return {"status": "failed", "code": error, "notice_sent": True}
        return {"status": "success", "sent_message_id": sent_message_id}

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
