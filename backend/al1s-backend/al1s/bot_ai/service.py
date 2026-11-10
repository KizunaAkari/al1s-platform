import asyncio
import contextlib
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from al1s.bot_ai.contracts import AiEnvelope, ProtocolError
from al1s.bot_ai.messages import AiIncoming, parse_ai_event, persona_command
from al1s.bot_ai.smart_reply import SmartReplyWindows, merge_fragments
from al1s.bot_ai.tools import BotAiTools
from al1s.meme_workshop.qq_scheduler import Admission, QqScheduler
from al1s.freqtrade.contracts import DEFAULT_COMMANDS as FREQTRADE_COMMANDS
from al1s.quant.commands import DEFAULT_COMMANDS as QUANT_COMMANDS


def _local_vision(settings, incoming):
    provider = getattr(settings, "providers", {}).get("vision")
    return bool(getattr(settings, "vision_enabled", False) and provider
                and provider.adapter_kind == "al1s_comfyui_vision"
                and any(part.get("type") == "image" for part in incoming.parts))


def _request_deadline(settings, incoming):
    return datetime.now(UTC) + timedelta(seconds=2030 if _local_vision(settings, incoming) else 120)


@dataclass
class _Context:
    envelope: AiEnvelope
    client: object
    future: asyncio.Future
    session_key: tuple
    blocked: bool = False
    native_status: str | None = None
    result_code: str | None = None


class BotAiService:
    def __init__(self, repository, hub, media, sender, quant_client, freqtrade_client,
                 meme_sender, *, stop_timeout: float = 5) -> None:
        self.repository, self.hub, self.media, self.sender = repository, hub, media, sender
        self.sticker_importer = None
        self.records = {}
        self.reserved = {}
        self._contexts: dict[UUID, _Context] = {}
        self._queued: dict[tuple[UUID, str], tuple] = {}
        self._notice_at: dict[UUID, float] = {}
        self._recovery_tasks: set[asyncio.Task] = set()
        self._paused: set[UUID] = set()
        self._clearing: set[tuple] = set()
        self._smart_windows = SmartReplyWindows()
        self._smart_pending: dict[tuple, tuple] = {}
        self._smart_seeds: dict[tuple, str] = {}
        self._smart_timers: dict[tuple, asyncio.Task] = {}
        self.stop_timeout = stop_timeout
        self.scheduler = QqScheduler(on_expire=self._expired)
        self.tools = BotAiTools(repository, hub, quant_client, freqtrade_client, media.root,
                                meme_sender)
        hub.set_handlers(on_tool=self.tools.execute, on_output=self._output,
                         on_completed=self._completed, on_disconnect=self._disconnected,
                         on_ready=self._ready)

    async def refresh(self):
        connections = await asyncio.to_thread(self.repository.qq_connections)
        records = await asyncio.to_thread(self.repository.trigger_snapshots,
                                           [row.service_id for row in connections])
        self.records = records
        defaults = {unicodedata.normalize("NFKC", word).casefold() for word in
                    [*FREQTRADE_COMMANDS.values(), *QUANT_COMMANDS.values(),
                     "随机表情", "表情列表", "头像合成"]}
        self.reserved = {row.service_id: defaults.copy() for row in connections}
        return connections

    async def accept(self, row, client, event: object) -> bool:
        if row.service_id in self._paused:
            return False
        settings = self.records.get(row.service_id)
        if settings is None or settings.event_ws_url != row.event_url:
            return False
        incoming = parse_ai_event(event, settings)
        if incoming is None:
            return False
        text = "".join(part["text"] for part in incoming.parts if part["type"] == "text").strip()
        if text and not incoming.sticker_collect:
            word = unicodedata.normalize("NFKC", text.split(maxsplit=1)[0]).casefold()
            if (word in self.reserved.get(row.service_id, set())
                    or await asyncio.to_thread(self.repository.is_reserved, row.service_id, word)):
                return True
        if incoming.verify_reply_id and not await self._verify_reply(client, incoming):
            return True
        key = (row.service_id, incoming.source.scope_kind, incoming.source.target_id)
        persona_name = persona_command(text)
        if key in self._clearing:
            if not incoming.smart or persona_name is not None or incoming.sticker_collect:
                await self._notice(row, client, incoming, settings, "会话正在清空，请稍后重试。")
            return True
        if row.service_id in self._paused:
            return True
        if incoming.smart and persona_name is None and not incoming.sticker_collect:
            self._queue_smart(row, client, incoming, settings, key)
            return True
        if persona_name is not None or incoming.sticker_collect:
            self._flush_smart(key)
        marker = (row.service_id, incoming.source.message_id)
        previous = self._queued.get(marker)
        self._queued[marker] = (row, client, incoming, settings)
        reply_all = incoming.source.scope_kind == "qq_group" and incoming.source.target_id in settings.reply_all_groups
        vision_request = persona_name is None and not incoming.sticker_collect and _local_vision(settings, incoming)
        result = self.scheduler.try_submit("ai_vision" if vision_request else "ai", key, incoming.source,
            lambda: self._collect_sticker(row, client, incoming, settings, key,
                                          datetime.now(UTC) + timedelta(seconds=120))
            if incoming.sticker_collect else self._switch_persona(row, client, incoming, settings, key,
                                        datetime.now(UTC) + timedelta(seconds=120), persona_name)
            if persona_name is not None else
            self._execute(row, client, incoming, settings, key, _request_deadline(settings, incoming)),
            per_session=12 if reply_all else None, queue_timeout=1800 if reply_all or vision_request else None)
        if result != Admission.ACCEPTED:
            if previous is None:
                self._queued.pop(marker, None)
            else:
                self._queued[marker] = previous
            if result == Admission.BUSY:
                await self._notice(row, client, incoming, settings, "AI 对话繁忙，请稍后重试。")
        return True

    def _queue_smart(self, row, client, incoming, settings, key):
        if not self._smart_windows.observe(key, incoming):
            return
        previous = self._smart_pending.get(key)
        if previous is not None and previous[3].applied_version == settings.applied_version:
            incoming = merge_fragments(previous[2], incoming)
        self._smart_pending[key] = (row, client, incoming, settings)
        if key in self._smart_seeds:
            return
        if incoming.addressed:
            self._flush_smart(key)
        elif key not in self._smart_timers:
            self._smart_timers[key] = asyncio.create_task(self._flush_smart_later(key))

    async def _flush_smart_later(self, key):
        try:
            await asyncio.sleep(2)
            self._flush_smart(key)
        finally:
            if self._smart_timers.get(key) is asyncio.current_task():
                self._smart_timers.pop(key, None)

    def _flush_smart(self, key):
        timer = self._smart_timers.pop(key, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        values = self._smart_pending.get(key)
        if values is None or key in self._smart_seeds:
            return
        row, _, incoming, settings = values
        seed = incoming.source.message_id
        self._smart_seeds[key] = seed
        self._queued[(row.service_id, seed)] = values
        result = self.scheduler.try_submit("ai_vision" if _local_vision(settings, incoming) else "ai", key, incoming.source,
            lambda: self._run_smart(key, seed), per_session=1,
            queue_timeout=1800 if _local_vision(settings, incoming) else 120)
        if result != Admission.ACCEPTED:
            self._drop_smart(key)

    async def _run_smart(self, key, seed):
        if self._smart_seeds.get(key) != seed:
            return
        self._smart_seeds.pop(key, None)
        self._queued.pop((key[0], seed), None)
        values = self._smart_pending.pop(key, None)
        if values is None:
            return
        row, client, incoming, settings = values
        incoming = replace(incoming, context=self._smart_windows.snapshot(key),
                           observed_images=self._smart_windows.take_images(key))
        await self._execute(row, client, incoming, settings, key, _request_deadline(settings, incoming))

    def _drop_smart(self, key):
        timer = self._smart_timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        seed = self._smart_seeds.pop(key, None)
        if seed is not None:
            self._queued.pop((key[0], seed), None)
        self._smart_pending.pop(key, None)

    async def reject_busy(self, row, client, event: object) -> None:
        settings = self.records.get(row.service_id)
        if settings is None or settings.event_ws_url != row.event_url:
            return
        incoming = parse_ai_event(event, settings)
        if incoming is None:
            return
        text = "".join(part["text"] for part in incoming.parts if part["type"] == "text").strip()
        if text and await asyncio.to_thread(self.repository.is_reserved, row.service_id,
                unicodedata.normalize("NFKC", text.split(maxsplit=1)[0]).casefold()):
            return
        if incoming.verify_reply_id and not await self._verify_reply(client, incoming):
            return
        if incoming.smart and persona_command(text) is None:
            return
        await self._notice(row, client, incoming, settings, "机器人繁忙，请稍后重试。")

    @staticmethod
    async def _verify_reply(client, incoming: AiIncoming) -> bool:
        try:
            result = await asyncio.wait_for(asyncio.to_thread(client.post, "get_msg", {
                "message_id": int(incoming.verify_reply_id),
            }), timeout=6)
        except (OSError, ValueError, TimeoutError):
            return False
        sender = result.get("sender") if isinstance(result, dict) else None
        return bool(isinstance(sender, dict)
            and str(sender.get("user_id")) == incoming.self_id
            and str(result.get("group_id")) == incoming.source.target_id
            and result.get("message_type") == "group")

    async def _notice(self, row, client, incoming, settings, content: str) -> None:
        now = time.monotonic()
        if now < self._notice_at.get(row.service_id, 0):
            return
        self._notice_at[row.service_id] = now + 1
        try:
            if await asyncio.to_thread(self.repository.allowed, row.service_id,
                                       incoming.source, settings.applied_version):
                await asyncio.wait_for(asyncio.to_thread(client.send, incoming.source, content), 6)
        except (OSError, ValueError, TimeoutError):
            pass

    async def _expired(self, item) -> None:
        marker = (item.session_key[0], item.source.message_id)
        entry = self._queued.pop(marker, None)
        if self._smart_seeds.get(item.session_key) == item.source.message_id:
            self._drop_smart(item.session_key)
            return
        if entry:
            await self._notice(*entry, "AI 对话排队超时，请稍后重试。")

    async def _collect_sticker(self, row, client, incoming, settings, key, deadline) -> None:
        self._queued.pop((row.service_id, incoming.source.message_id), None)
        if row.service_id in self._paused or key in self._clearing:
            return
        if not await asyncio.to_thread(self.repository.allowed, row.service_id, incoming.source,
                                       settings.applied_version, admission=True):
            return
        request_id = await asyncio.to_thread(self.repository.claim, row.service_id, incoming.source,
                                             settings.applied_version, datetime.now(UTC))
        if request_id is None:
            return
        if not await asyncio.to_thread(self.repository.activate, request_id, "sticker-command"):
            await asyncio.to_thread(self.repository.fail_queued, request_id, "ai_session_busy")
            await self._notice(row, client, incoming, settings, "当前会话正在恢复，请稍后重试。")
            return
        try:
            content = await self._collect_sticker_content(row, client, incoming, settings, key, request_id)
            if content is None:
                await asyncio.to_thread(self.repository.confirm_finished, request_id, "interrupted")
                return
            if not content:
                await asyncio.to_thread(self.repository.confirm_finished, request_id, "completed")
                return
            envelope = AiEnvelope(request_id=request_id, service_id=row.service_id,
                source=incoming.source, self_id=incoming.self_id, sender_name=incoming.sender_name,
                parts=({"type": "text", "text": content},), config_version=settings.applied_version,
                deadline=deadline)
            await self.sender.send(envelope, 0, [{"type": "text", "text": content}], client,
                is_active=lambda: row.service_id not in self._paused and key not in self._clearing)
            pending = self.sender.pending_send(request_id)
            if pending is not None:
                await asyncio.to_thread(self.repository.block, request_id, reason="send_pending")
                self.scheduler.block(key)
                task = asyncio.create_task(self._finish_persona_send(request_id, key, pending))
                self._recovery_tasks.add(task)
                task.add_done_callback(self._recovery_tasks.discard)
            else:
                await asyncio.to_thread(self.repository.confirm_finished, request_id, "completed")
        except asyncio.CancelledError:
            await asyncio.to_thread(self.repository.block, request_id)
            self.scheduler.block(key)
            raise
        except (OSError, ValueError, TimeoutError):
            await asyncio.to_thread(self.repository.confirm_finished, request_id, "failed")

    async def _collect_sticker_content(self, row, client, incoming, settings, key, request_id):
        images = [part["reference"] for part in incoming.parts if part["type"] == "image"]
        reply = incoming.collect_reply_id
        if incoming.collect_reply_invalid or len(images) > 1 or (reply and images):
            return "请只引用或附带一张图片，再 @我发送“收藏”。"
        if reply:
            try:
                message = await asyncio.wait_for(asyncio.to_thread(client.post, "get_msg", {
                    "message_id": int(reply),
                }), timeout=6)
            except (OSError, ValueError, TimeoutError):
                return "无法读取引用的图片，请重新引用后再试。"
            if (not isinstance(message, dict) or message.get("message_type") != "group"
                    or str(message.get("group_id")) != incoming.source.target_id):
                return "只能收藏同一群里引用的图片。"
            segments = message.get("message")
            if not isinstance(segments, list) or len(segments) > 128:
                return "无法读取引用的图片，请重新引用后再试。"
            images = []
            for segment in segments:
                if (isinstance(segment, dict) and segment.get("type") in {"image", "mface"}
                        and isinstance(segment.get("data"), dict)):
                    reference = segment["data"].get("url") or segment["data"].get("file")
                    if not isinstance(reference, str) or len(reference) > 2048:
                        return "无法读取引用的图片，请重新引用后再试。"
                    images.append(reference)
        if len(images) != 1:
            return "请只引用或附带一张图片，再 @我发送“收藏”。"
        if not settings.vision_enabled or self.sticker_importer is None:
            return "图片理解未启用，暂时无法收藏，请联系管理员。"
        media_task = asyncio.create_task(asyncio.to_thread(self.media.prepare, request_id,
            [{"type": "image", "reference": images[0]}], client))
        try:
            try:
                prepared = await asyncio.wait_for(asyncio.shield(media_task), timeout=20)
            except (OSError, ValueError, TimeoutError):
                return "图片读取失败，请重新选择后再试。"
            if (row.service_id in self._paused or key in self._clearing or
                    not await asyncio.to_thread(self.repository.allowed, row.service_id,
                                                incoming.source, settings.applied_version)):
                return None
            reference = prepared[0].get("original_ref") if prepared else None
            if not isinstance(reference, str):
                return "图片读取失败，请重新选择后再试。"
            try:
                result = await self.sticker_importer(row.service_id, incoming.source.target_id,
                                                     request_id, reference)
            except (OSError, ValueError, TimeoutError):
                return "收藏暂不可用，请检查图片理解服务或稍后重试。"
            if result.get("status") == "queued":
                return ""
            if result.get("status") == "full":
                return "表情图库已满，请管理员整理后再收藏。"
            return "识别队列繁忙，请稍后重试。"
        finally:
            if not media_task.done():
                media_task.add_done_callback(lambda task: self._late_media_cleanup(request_id, task))
            else:
                with contextlib.suppress(OSError, ValueError):
                    await asyncio.to_thread(self.media.cleanup, request_id)

    async def _switch_persona(self, row, client, incoming, settings, key, deadline, name: str) -> None:
        self._queued.pop((row.service_id, incoming.source.message_id), None)
        if row.service_id in self._paused or key in self._clearing:
            return
        if not await asyncio.to_thread(self.repository.allowed, row.service_id, incoming.source,
                                       settings.applied_version, admission=True):
            return
        request_id = await asyncio.to_thread(self.repository.claim, row.service_id, incoming.source,
                                             settings.applied_version, datetime.now(UTC))
        if request_id is None:
            return
        if not await asyncio.to_thread(self.repository.activate, request_id, "persona-command"):
            await asyncio.to_thread(self.repository.fail_queued, request_id, "ai_session_busy")
            await self._notice(row, client, incoming, settings, "当前会话正在恢复，请稍后重试。")
            return
        try:
            if row.service_id in self._paused or key in self._clearing:
                await asyncio.to_thread(self.repository.confirm_finished, request_id, "interrupted")
                return
            try:
                if name:
                    profile = await asyncio.to_thread(self.repository.select_chat_profile,
                        row.service_id, incoming.source, name, settings.applied_version)
                    content = f"已切换到人格：{profile.name}。"
                else:
                    profiles = await asyncio.to_thread(self.repository.list_chat_profiles, row.service_id)
                    current = await asyncio.to_thread(self.repository.selected_chat_profile,
                        row.service_id, incoming.source.scope_kind, incoming.source.target_id,
                        version=settings.applied_version)
                    names = [profile.name for profile in profiles if profile.secret_configured or profile.is_default]
                    content = (f"当前人格：{current.name if current else '未配置'}。\n"
                               f"可切换：{'、'.join(names) if names else '暂无可用人格'}。\n"
                               "发送“切换人格 名称”进行切换。")
            except ValueError as error:
                if str(error) == "ai_scope_denied":
                    await asyncio.to_thread(self.repository.confirm_finished, request_id, "interrupted")
                    return
                content = ("该人格尚未配置密钥，请管理员先完成配置。"
                           if str(error) == "ai_profile_unconfigured" else
                           "未找到可切换的人格，请发送“切换人格”查看列表。")
            envelope = AiEnvelope(request_id=request_id, service_id=row.service_id,
                source=incoming.source, self_id=incoming.self_id, sender_name=incoming.sender_name,
                parts=({"type": "text", "text": content},), config_version=settings.applied_version,
                deadline=deadline)
            await self.sender.send(envelope, 0, [{"type": "text", "text": content}], client,
                is_active=lambda: row.service_id not in self._paused and key not in self._clearing)
            pending = self.sender.pending_send(request_id)
            if pending is not None:
                await asyncio.to_thread(self.repository.block, request_id, reason="send_pending")
                self.scheduler.block(key)
                task = asyncio.create_task(self._finish_persona_send(request_id, key, pending))
                self._recovery_tasks.add(task)
                task.add_done_callback(self._recovery_tasks.discard)
            else:
                await asyncio.to_thread(self.repository.confirm_finished, request_id, "completed")
        except asyncio.CancelledError:
            await asyncio.to_thread(self.repository.block, request_id)
            self.scheduler.block(key)
            raise
        except (OSError, ValueError, TimeoutError):
            await asyncio.to_thread(self.repository.confirm_finished, request_id, "failed")

    async def _finish_persona_send(self, request_id, key, pending) -> None:
        with contextlib.suppress(Exception):
            await asyncio.shield(pending)
        if await asyncio.to_thread(self.repository.confirm_finished, request_id, "completed"):
            self.scheduler.unblock(key)

    async def _execute(self, row, client, incoming: AiIncoming, settings, key, deadline) -> None:
        self._queued.pop((row.service_id, incoming.source.message_id), None)
        if row.service_id in self._paused or key in self._clearing:
            return
        if not await asyncio.to_thread(self.repository.allowed, row.service_id,
                                       incoming.source, settings.applied_version, admission=True):
            return
        if row.service_id in self._paused or key in self._clearing:
            return
        request_id = await asyncio.to_thread(self.repository.claim, row.service_id,
            incoming.source, settings.applied_version, datetime.now(UTC))
        if request_id is None:
            return
        if row.service_id in self._paused or key in self._clearing:
            await asyncio.to_thread(self.repository.fail_queued, request_id, "ai_configuration_paused")
            return
        epoch = self.hub.epoch(row.service_id)
        if epoch is None:
            await asyncio.to_thread(self.repository.fail_queued, request_id, "ai_engine_unavailable")
            await self._notice(row, client, incoming, settings, "AI 对话服务暂不可用，请稍后重试。")
            return
        if not await asyncio.to_thread(self.repository.activate, request_id, epoch):
            await asyncio.to_thread(self.repository.fail_queued, request_id, "ai_session_busy")
            await self._notice(row, client, incoming, settings, "当前会话正在恢复，请稍后重试。")
            return
        context, media_task = None, None
        try:
            if row.service_id in self._paused or key in self._clearing:
                await asyncio.to_thread(self.repository.confirm_finished, request_id, "interrupted")
                return
            profile = await asyncio.to_thread(self.repository.selected_chat_profile,
                row.service_id, incoming.source.scope_kind, incoming.source.target_id,
                version=settings.applied_version)
            if profile is None or (not profile.secret_configured and not profile.is_default):
                raise ValueError("ai_profile_unconfigured")
            types = {part["type"] for part in incoming.parts}
            if ("image" in types and not settings.vision_enabled) or (
                    "record" in types and not settings.stt_enabled):
                raise ValueError("ai_media_disabled")
            vision = settings.providers.get("vision")
            separate_vision = bool(vision and vision.adapter_kind != "follow_chat" and vision.configured)
            if "image" in types and not profile.supports_vision and not separate_vision:
                raise ValueError("ai_profile_vision_unavailable")
            media_options = {"observations": incoming.observed_images} if (
                incoming.observed_images and settings.vision_enabled and
                (profile.supports_vision or separate_vision)) else {}
            media_task = asyncio.create_task(asyncio.to_thread(
                self.media.prepare, request_id, list(incoming.parts), client, **media_options))
            parts = await asyncio.wait_for(asyncio.shield(media_task), timeout=20)
            if row.service_id in self._paused or key in self._clearing:
                await asyncio.to_thread(self.repository.confirm_finished, request_id, "interrupted")
                return
            envelope = AiEnvelope(request_id=request_id, service_id=row.service_id,
                source=incoming.source, self_id=incoming.self_id, sender_name=incoming.sender_name,
                mentions=incoming.mentions, parts=parts, config_version=settings.applied_version,
                chat_profile_id=profile.id,
                persona_prompt=profile.persona or "你是 QQ 群里的助理。尊重发言者身份，简洁准确地回答。",
                tts_enabled=(settings.tts_mode == "always" or
                             (settings.tts_mode == "voice_input" and "record" in types)),
                smart_reply=settings.smart_reply if incoming.smart else None,
                sticker_enabled=(incoming.source.scope_kind == "qq_group" and settings.smart_reply.stickers_enabled),
                addressed=incoming.addressed,
                smart_context=incoming.context,
                vision_provider_id=(f"al1s_{row.service_id.hex}_{settings.applied_version}_vision"
                    if separate_vision else f"al1s_{row.service_id.hex}_{settings.applied_version}_chat_{profile.id.hex}")
                    if settings.vision_enabled and (separate_vision or profile.supports_vision) else None,
                deadline=deadline)
            future = asyncio.get_running_loop().create_future()
            context = _Context(envelope, client, future, key)
            self._contexts[request_id] = context
            await self.hub.dispatch(envelope)
            remaining = max(0, (deadline - datetime.now(UTC)).total_seconds())
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
                if result == "failed" and not incoming.smart and context.result_code:
                    messages = {"gpu_queue_full": "图片任务排队已满，请稍后重试。",
                                "gpu_user_busy": "你的上一项图片任务还没有结束。",
                                "comfy_timeout": "图片理解执行超时，请稍后重试。",
                                "gpu_vision_disabled": "图片理解已关闭，等待中的识图请求已取消。",
                                "comfy_resource_unavailable": "图片理解服务暂不可用，请检查共享服务。",
                                "comfy_cleanup_pending": "图片任务正在恢复，请稍后重试。"}
                    message = messages.get(context.result_code)
                    if message:
                        await self._notice(row, client, incoming, settings, message)
            except TimeoutError:
                with contextlib.suppress(ProtocolError):
                    self.hub.revoke(request_id)
                await asyncio.to_thread(self.repository.begin_stop, request_id)
                with contextlib.suppress(ProtocolError):
                    await self.hub.cancel(request_id)
                try:
                    await asyncio.wait_for(asyncio.shield(future), timeout=self.stop_timeout)
                except TimeoutError:
                    await asyncio.to_thread(self.repository.block, request_id)
                    if not future.done():
                        context.blocked = True
                        self.scheduler.block(key)
        except asyncio.CancelledError:
            if context is not None:
                with contextlib.suppress(ProtocolError):
                    self.hub.revoke(request_id)
            await asyncio.to_thread(self.repository.block, request_id)
            if context is not None:
                context.blocked = True
                self.scheduler.block(key)
                with contextlib.suppress(ProtocolError):
                    await self.hub.cancel(request_id)
            raise
        except (OSError, ValueError, TimeoutError) as error:
            await asyncio.to_thread(self.repository.confirm_finished, request_id, "failed")
            message = ("当前人格的模型不支持图片，请配置支持图片的模型或独立图片理解。"
                       if str(error) == "ai_profile_vision_unavailable" else
                       "AI 对话或媒体暂不可用，请稍后重试。")
            if not incoming.smart:
                await self._notice(row, client, incoming, settings, message)
        finally:
            if context is None or not context.blocked:
                self._contexts.pop(request_id, None)
                self.tools.forget(request_id)
                if media_task is not None and not media_task.done():
                    media_task.add_done_callback(lambda task: self._late_media_cleanup(request_id, task))
                else:
                    with contextlib.suppress(OSError, ValueError):
                        await asyncio.to_thread(self.media.cleanup, request_id)

    def _late_media_cleanup(self, request_id: UUID, task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()
        cleanup = asyncio.create_task(asyncio.to_thread(self.media.cleanup, request_id))
        self._recovery_tasks.add(cleanup)
        cleanup.add_done_callback(self._recovery_tasks.discard)

    async def _output(self, envelope: AiEnvelope, sequence: int, parts: list) -> dict:
        context = self._contexts.get(envelope.request_id)
        if context is None or context.blocked or not self.hub.is_active(envelope.request_id):
            return {"status": "denied", "code": "ai_request_stale"}
        text_parts = [(index, part) for index, part in enumerate(parts) if part.get("type") == "text"]
        if len(text_parts) == 1 and len(parts) < 5:
            index, part = text_parts[0]
            text = part.get("text", "").strip()
            paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
            if len(paragraphs) == 2 and all(len(line) <= 1000 for line in paragraphs) and "`" not in text:
                parts = [*parts[:index], *({**part, "text": line} for line in paragraphs), *parts[index + 1:]]
            elif 25 < len(text) <= 50 and not any(value in text for value in ("http://", "https://", "`")):
                choices = []
                for cut, character in enumerate(text[:-1], 1):
                    if character in "。！？!?；;，,\n":
                        left, right = text[:cut].strip(), text[cut:].strip()
                        if 0 < len(left) <= 25 and 0 < len(right) <= 25:
                            choices.append((character not in "。！？!?\n", abs(len(left) - len(right)), left, right))
                if choices:
                    _, _, left, right = min(choices)
                    parts = [*parts[:index], {**part, "text": left}, {**part, "text": right}, *parts[index + 1:]]
        guard = getattr(self.hub, 'output_guard', lambda _identity: None)(envelope.request_id)
        options = {'image_allowed': guard[0], 'fallback_text': guard[1]} if guard else {}
        return await self.sender.send(envelope, sequence, parts, context.client,
                                      is_active=lambda: self.hub.is_active(envelope.request_id), **options)

    async def _completed(self, envelope: AiEnvelope, status: str, result_code: str | None = None) -> None:
        context = self._contexts.get(envelope.request_id)
        if context is None:
            return
        context.result_code = result_code
        meme_pending = self.tools.pending_meme(envelope.request_id)
        send_pending = self.sender.pending_send(envelope.request_id)
        pending = [task for task in (meme_pending, send_pending) if task is not None]
        if pending:
            await asyncio.to_thread(self.repository.block, envelope.request_id,
                                    reason="meme_pending" if meme_pending else "send_pending")
            context.blocked = True
            context.native_status = status
            self.scheduler.block(context.session_key)
            if not context.future.done():
                context.future.set_result("blocked")
            task = asyncio.create_task(self._finish_after_effects(envelope, status, pending, result_code))
            self._recovery_tasks.add(task)
            task.add_done_callback(self._recovery_tasks.discard)
            return
        detail = {"result_code": result_code} if result_code is not None else {}
        if await asyncio.to_thread(self.repository.confirm_finished, envelope.request_id, status, **detail):
            if context.blocked:
                self.scheduler.unblock(context.session_key)
                context.blocked = False
                self._contexts.pop(envelope.request_id, None)
                self.tools.forget(envelope.request_id)
                with contextlib.suppress(OSError, ValueError):
                    await asyncio.to_thread(self.media.cleanup, envelope.request_id)
            if not context.future.done():
                context.future.set_result(status)

    async def _finish_after_effects(self, envelope, status, pending, result_code=None) -> None:
        for task in pending:
            with contextlib.suppress(Exception):
                await asyncio.shield(task)
        await self._completed(envelope, status, result_code)

    async def _disconnected(self, envelope: AiEnvelope) -> None:
        context = self._contexts.get(envelope.request_id)
        if context is None:
            return
        blocked = await asyncio.to_thread(self.repository.block, envelope.request_id)
        if not blocked or context.future.done():
            return
        context.blocked = True
        self.scheduler.block(context.session_key)
        if not context.future.done():
            context.future.set_result("blocked")

    async def _ready(self, service_id: UUID, _version: int) -> None:
        rows = await asyncio.to_thread(self.repository.blocked_sessions, service_id)
        for request_id, scope, target in rows:
            context = self._contexts.get(request_id)
            if context is not None and context.native_status is not None:
                continue
            task = asyncio.create_task(self._recover(request_id, (service_id, scope, target)))
            self._recovery_tasks.add(task)
            task.add_done_callback(self._recovery_tasks.discard)

    async def _recover(self, request_id: UUID, key: tuple) -> None:
        for _ in range(40):
            if (self.tools.pending_meme(request_id) is not None
                    or self.sender.pending_send(request_id) is not None):
                await asyncio.sleep(0.25)
                continue
            if await asyncio.to_thread(self.repository.confirm_finished, request_id, "interrupted"):
                self.scheduler.unblock(key)
                context = self._contexts.pop(request_id, None)
                if context and not context.future.done():
                    context.future.set_result("interrupted")
                self.tools.forget(request_id)
                with contextlib.suppress(OSError, ValueError):
                    await asyncio.to_thread(self.media.cleanup, request_id)
                return
            await asyncio.sleep(0.25)
        for pending in (self.tools.pending_meme(request_id), self.sender.pending_send(request_id)):
            if pending is not None:
                pending.add_done_callback(lambda _task: self._schedule_recovery(request_id, key))

    def _schedule_recovery(self, request_id: UUID, key: tuple) -> None:
        task = asyncio.create_task(self._recover(request_id, key))
        self._recovery_tasks.add(task)
        task.add_done_callback(self._recovery_tasks.discard)

    def pause(self, service_id: UUID) -> None:
        self._paused.add(service_id)
        for key in tuple(self._smart_pending):
            if key[0] == service_id:
                self._drop_smart(key)
        self._smart_windows.forget_service(service_id)

    def cancel_waiting_vision(self, service_id: UUID) -> None:
        for item in self.scheduler.drop_vision(service_id):
            entry = self._queued.pop((service_id, item.source.message_id), None)
            if self._smart_seeds.get(item.session_key) == item.source.message_id:
                self._drop_smart(item.session_key)
            if entry and not entry[2].smart:
                task = asyncio.create_task(self._notice(*entry, "图片理解已关闭，等待中的识图请求已取消。"))
                self._recovery_tasks.add(task)
                task.add_done_callback(self._recovery_tasks.discard)

    def resume(self, service_id: UUID) -> None:
        self._paused.discard(service_id)

    async def wait_idle(self, service_id: UUID, timeout: float = 130) -> bool:
        try:
            async with asyncio.timeout(timeout):
                while (self.scheduler.active_for_service(service_id) or any(
                        context.envelope.service_id == service_id for context in self._contexts.values())):
                    await asyncio.sleep(0.1)
            return True
        except TimeoutError:
            return False

    async def clear_session(self, service_id: UUID, scope: str, target: str) -> dict:
        key = (service_id, scope, target)
        if key in self._clearing:
            raise ValueError("ai_session_busy")
        self._clearing.add(key)
        self._drop_smart(key)
        self._smart_windows.forget(key)
        deferred = False
        try:
            for item in self.scheduler.drop_pending(key):
                self._queued.pop((service_id, item.source.message_id), None)
            for context in tuple(self._contexts.values()):
                if context.session_key == key:
                    with contextlib.suppress(ProtocolError):
                        self.hub.revoke(context.envelope.request_id)
                    await asyncio.to_thread(self.repository.begin_stop, context.envelope.request_id)
                    with contextlib.suppress(ProtocolError):
                        await self.hub.cancel(context.envelope.request_id)
            try:
                async with asyncio.timeout(self.stop_timeout):
                    while (self.scheduler.active(key) or any(
                            context.session_key == key for context in self._contexts.values())):
                        await asyncio.sleep(0.1)
            except TimeoutError as exc:
                for context in tuple(self._contexts.values()):
                    if context.session_key == key and not context.future.done():
                        await asyncio.to_thread(self.repository.block, context.envelope.request_id)
                        if not context.future.done():
                            context.blocked = True
                            self.scheduler.block(key)
                            context.future.set_result("blocked")
                deferred = True
                task = asyncio.create_task(self._finish_clear_wait(key))
                self._recovery_tasks.add(task)
                task.add_done_callback(self._recovery_tasks.discard)
                raise ValueError("ai_clear_unconfirmed") from exc
            return await self.hub.clear_session(service_id, scope, target)
        finally:
            if not deferred:
                self._clearing.discard(key)

    async def _finish_clear_wait(self, key: tuple) -> None:
        while (self.scheduler.active(key) or any(
                context.session_key == key for context in self._contexts.values())):
            await asyncio.sleep(0.1)
        self._clearing.discard(key)

    async def stop(self) -> None:
        timers = tuple(self._smart_timers.values())
        for key in tuple(self._smart_pending):
            self._drop_smart(key)
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        self._smart_windows.clear()
        await self.scheduler.stop()
        for task in tuple(self._recovery_tasks):
            task.cancel()
        if self._recovery_tasks:
            await asyncio.gather(*tuple(self._recovery_tasks), return_exceptions=True)
