import asyncio
import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid5

import httpx
from al1s.bot_ai.contracts import AiEnvelope
from al1s.bot_ai.drawing_inference import decode_interpretation, explicit_prompt, explicit_tag_prompt, split_negative_marker
from al1s.meme_workshop.onebot import identity
from .commands import DrawingParseError, parse_drawing_event, resolve_reference

ENDPOINT_KEY = "comfy-local-8188"


class _ControlReceipt:
    def __init__(self, repository, service_id, key, source, version):
        self.repository, self.service_id, self.key, self.source, self.version = repository, service_id, key, source, version

    def allowed(self, service_id, source, version):
        return self.repository.allowed(service_id, source, version)

    def start_send(self, _request, _kind):
        return True

    def mark_transport_started(self, _request, _kind, digest):
        return digest is None and self.repository.start_control(self.service_id, self.key, self.source, self.version)

    def finish_send(self, _request, _kind, status, _count, _receipt):
        if status in {"sent", "unknown"}:
            self.repository.finish_control(self.service_id, self.key, status)


class DrawingService:
    def __init__(self, repository, manager, media, sender, runner):
        self.repository, self.manager = repository, manager
        self.media, self.sender, self.runner = media, sender, runner
        self._identities = {}

    async def refresh(self):
        return await asyncio.to_thread(self.repository.qq_connections)

    async def _identity(self, service_id, client):
        previous = self._identities.get(service_id)
        if previous is not None and previous[0] is client:
            return previous[1]
        try:
            result = await asyncio.wait_for(asyncio.to_thread(client.post, "get_login_info", {}), timeout=6)
            own = identity(result.get("user_id")) if isinstance(result, dict) else None
        except (OSError, ValueError, TimeoutError):
            return None
        if own:
            self._identities[service_id] = (client, own)
        return own

    async def _control(self, row, client, command, version, action, text, *, workflow_number=None):
        try:
            key, receipt = await asyncio.to_thread(self.repository.claim_control, row.service_id, command.source,
                command.self_id, action, text, workflow_number=workflow_number)
        except ValueError as error:
            if str(error) not in {"drawing_selection_unavailable", "drawing_group_preset_denied"} or workflow_number is None:
                return
            if str(error) == "drawing_group_preset_denied":
                key, receipt = await asyncio.to_thread(self.repository.claim_control, row.service_id, command.source,
                    command.self_id, action, "该群未启用此工作流。")
                await self._send_control(row, client, command, version, key, receipt)
                return
            key, receipt = await asyncio.to_thread(self.repository.claim_control, row.service_id, command.source,
                command.self_id, action, "该工作流未启用或尚未完成验证，请使用 /wf 查看。")
        await self._send_control(row, client, command, version, key, receipt)

    async def _send_control(self, row, client, command, version, key, receipt):
        if receipt.get("status") != "claimed" or self.sender is None:
            return
        envelope = AiEnvelope(request_id=uuid5(row.service_id, key), service_id=row.service_id,
            source=command.source, self_id=command.self_id, sender_name="群友",
            parts=[{"type": "text", "text": "绘图指令"}], config_version=version,
            deadline=datetime.now(UTC) + timedelta(seconds=30))
        adapter = _ControlReceipt(self.repository, row.service_id, key, command.source, version)
        await self.sender.send(envelope, 0, [{"type": "text", "text": receipt["text"]}], client,
                               receipt_repository=adapter, receipt_kind="result")

    async def _workflow(self, row, client, command, version):
        number = command.workflow_number
        if number is None:
            presets = await asyncio.to_thread(self.repository.list_scope_presets, row.service_id, command.source)
            presets = [item for item in presets if item.get("engine", "comfy") == "comfy"]
            lines = [f"#{item['number']} {item['name']}（{'文生图' if item['kind'] == 'txt2img' else '图生图'}，"
                     f"{'可用' if item['enabled'] and item['validation_state'] == 'ready' else '未就绪'}）"
                     for item in presets]
            text = "工作流：\n" + "\n".join(lines) if lines else "当前没有可用的工作流。"
            text += "\n发送 /wfN 描述 直接绘图；回复单图发送 /p2p 描述 可用默认图生图。"
            await self._control(row, client, command, version, "/wf", text)
        else:
            text = ("已重置个人工作流选择。" if number == 0 else
                    f"已选择工作流 #{number}。实际绘图请在同一条消息发送 /wf{number} 描述。")
            await self._control(row, client, command, version, f"/wf{number}", text, workflow_number=number)

    @staticmethod
    def _task_id(service_id, command):
        source = command.source
        return uuid5(service_id, f"drawing:{command.self_id}:{source.scope_kind}:{source.target_id}:{source.message_id}")

    def _admit_with_input(self, task_id, body, admit):
        if body is None:
            return admit({"task_id": task_id})
        with self.media.staging():
            reference, checksum = self.media.put(task_id, "input", body)
            fields = {"task_id": task_id, "input_reference": reference,
                      "input_sha256": checksum, "input_bytes": len(body)}
            return admit(fields)

    async def _admit_queued(self, task_id, owner, admit):
        queue = getattr(self.runner, "gpu_queue", None)
        reserved = False
        if queue is not None:
            await queue.wait_recovery()
            try:
                reserved = queue.reserve(str(task_id), owner=owner)
            except ValueError as error:
                code = {"gpu_queue_full": "drawing_queue_full"}
                raise ValueError(code.get(str(error), str(error))) from error
        pending = asyncio.create_task(asyncio.to_thread(admit))
        try:
            try:
                result = await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending
                self.runner.wake()
                raise
        except asyncio.CancelledError:
            if pending.cancelled() and reserved:
                queue.finish(str(task_id))
            raise
        except Exception:
            if reserved:
                queue.finish(str(task_id))
            raise
        self.runner.wake()
        if reserved:
            if str(result) != str(task_id):
                queue.finish(str(task_id))
            else:
                task = await asyncio.to_thread(self.repository.task_for_worker, result)
                if (task["generation_status"] in {"succeeded", "failed", "cancelled", "expired"}
                        and task["cleanup_status"] == "done"):
                    queue.finish(str(task_id))
        return result

    async def submit_preview(self, service_id, preset_id, *, positive, negative, mode, image,
                             expected_version, operation_id):
        positive, marked_negative = split_negative_marker(positive)
        if not isinstance(negative, str):
            raise ValueError("drawing_request_invalid")
        if marked_negative:
            negative = ", ".join(part for part in (marked_negative, negative.strip()) if part)
        preset = await asyncio.to_thread(self.repository.get_preset, service_id, preset_id)
        if preset["row_version"] != expected_version:
            raise ValueError("drawing_preset_conflict")
        if preset.get("engine") == "openai":
            task_id = uuid5(service_id, "drawing-preview:" + operation_id)
            return await self._admit_online_queued(task_id, lambda: self._admit_with_input(task_id, image,
                lambda fields: self.repository.admit_online(service_id, preset_id=preset_id,
                    expected_version=expected_version, operation_id=operation_id, positive=positive,
                    negative=negative, mode=mode, **fields)))
        if (preset["kind"] == "image_edit") != (image is not None):
            raise ValueError("drawing_image_required")
        task_id = uuid5(service_id, "drawing-preview:" + operation_id)
        seed = int.from_bytes(hashlib.sha256(task_id.bytes).digest()[:8], "big") & ((1 << 63) - 1)
        return await self._admit_queued(task_id, None, lambda: self._admit_with_input(task_id, image,
            lambda fields: self.repository.admit_preview(service_id, preset_id, ENDPOINT_KEY,
                positive=positive, negative=negative, seed=seed, mode=mode, expected_version=expected_version,
                operation_id=operation_id, **fields)))

    async def accept(self, row, client, event) -> bool:
        own = identity(event.get("self_id")) if isinstance(event, dict) else None
        if not own:
            return False
        parse_error = None
        try:
            command = parse_drawing_event(event, own)
        except DrawingParseError as error:
            command, parse_error = error.command, str(error)
        except ValueError:
            return False
        if command is None:
            return False
        settings = await asyncio.to_thread(self.repository.get_settings, row.service_id)
        if (not settings["enabled"] or not settings["applied_version"]
                or settings["row_version"] != settings["applied_version"]
                or settings["event_ws_url"] != row.event_url):
            return False
        version = settings["applied_version"]
        if not await asyncio.to_thread(self.repository.allowed, row.service_id, command.source, version):
            return False
        if await self._identity(row.service_id, client) != command.self_id:
            return False
        if await asyncio.to_thread(self.repository.find_source, row.service_id, command.source, command.self_id):
            return True
        previous = await asyncio.to_thread(self.repository.find_control, row.service_id, command.source, command.self_id)
        if previous is not None:
            await self._send_control(row, client, command, version, *previous)
            return True
        if parse_error is not None:
            messages = {'p2p': '请回复当前会话中的单张图片，并在 /p2p 后说明修改要求。',
                        'tag': '请回复当前会话的一张图片并发送 /tag；群聊中还需要 @机器人。',
                        'workflow': '工作流切换指令不需要附带图片或引用。',
                        'workflow_draw': '请发送 /wf编号 描述；图生图可回复当前会话中的单张图片。',
                        'online_draw': '请发送 /gpt 描述 或 /gpt编号 描述。'}
            await self._control(row, client, command, version, 'drawing_error', messages[command.operation])
            return True
        if command.operation == "workflow":
            await self._workflow(row, client, command, version)
            return True
        if command.operation == "tag" and not command.reference_message_id:
            await self._control(row, client, command, version, "/tag", "请回复当前会话的一张图片并发送 /tag；群聊中还需要 @机器人。")
            return True
        if command.operation == "p2p" and (not command.prompt or not command.reference_message_id):
            await self._control(row, client, command, version, "/p2p", "请回复当前会话中的单张图片，并在 /p2p 后说明要怎样修改。")
            return True
        if command.operation == "online_draw" and not command.prompt:
            await self._control(row, client, command, version, "/gpt", "请发送 /gpt 描述 或 /gpt编号 描述。")
            return True
        try:
            body = await resolve_reference(command, client)
            if command.operation == "tag":
                prepared = await asyncio.to_thread(self.repository.prepare_tag, row.service_id, command.source)
                task_id = self._task_id(row.service_id, command)
                await self._admit_queued(task_id, (row.service_id, command.source.sender_id),
                    lambda: self._admit_with_input(task_id, body,
                        lambda fields: self.repository.admit_tag(row.service_id, command.source, command.self_id,
                            ENDPOINT_KEY, prepared, **fields, reference_message_id=command.reference_message_id)))
            elif command.operation == "online_draw":
                positive, negative = split_negative_marker(command.prompt)
                if not positive.strip():
                    raise ValueError("drawing_intent_invalid")
                original = {"original_positive": command.prompt} if positive != command.prompt else {}
                task_id = self._task_id(row.service_id, command)
                await self._admit_online_queued(task_id, lambda: self._admit_with_input(task_id, body,
                    lambda fields: self.repository.admit_online(row.service_id, source=command.source,
                        bot_self_id=command.self_id, preset_number=command.workflow_number,
                        positive=positive, negative=negative, reference_message_id=command.reference_message_id,
                        **original, **fields)))
                return True
            else:
                return await self._drawing(row, client, command, settings, body)
            return True
        except ValueError as error:
            code = str(error) if re.fullmatch(r'[a-z0-9_]{1,80}', str(error)) else 'drawing_processing_failed'
            logging.getLogger(__name__).warning('drawing_request_failed:%s', code)
            messages = {"drawing_scope_denied": "当前会话没有绘图权限。", "drawing_image_sender_invalid": "请回复当前会话中的单张图片。",
                "drawing_group_preset_denied": "该群未启用此工作流。",
                "drawing_image_scope_invalid": "只能使用当前会话中的图片。", "drawing_image_source_invalid": "请只引用一张图片。",
                "drawing_image_invalid": "请使用不超过 8 MiB 的 PNG、JPEG 或 WebP 静态图片。",
                "drawing_queue_full": "绘图队列已满，请稍后重试。",
                "drawing_media_full": "绘图存储空间已满，请联系管理员清理历史任务。",
                "drawing_route_changed": "工作流或绘图配置刚刚发生变化，请重新发送。",
                "drawing_negative_unclear": "请把直接提示词写成“正面：…… 负面：……”，以免误分条件。",
                "drawing_intent_invalid": "无法识别绘图要求，请换一种说法后重试。",
                "drawing_wish_invalid": "整理绘图描述失败，请换一种说法后重试。",
                "drawing_llm_timeout": "绘图提示词模型响应超时，请稍后重试。",
                "drawing_llm_provider_error": "绘图提示词模型请求失败，请检查模型配置或网络后重试。",
                "drawing_prompt_model_unconfigured": "请先在 AI 绘图通用设置中配置提示词模型，或直接提供正负面提示词。",
                "drawing_online_preset_unavailable": "该线上绘图预设不可用，请联系管理员检查默认预设或编号。",
                "drawing_online_unconfigured": "线上绘图模型尚未配置密钥，请联系管理员。",
                "meme_media_unavailable": "暂时无法读取引用图片，请重新发送或更换图片。",
                "meme_media_source_rejected": "无法读取该图片来源，请重新发送图片。"}
            await self._control(row, client, command, version, "drawing_error", messages.get(str(error), "绘图暂不可用，请检查工作流和模型配置。"))
            return True
        except (httpx.HTTPError, OSError, TimeoutError):
            await self._control(row, client, command, version, "drawing_error", "绘图暂不可用，请检查模型服务和连接后重试。")
            return True

    async def _admit_online_queued(self, task_id, admit):
        pending = asyncio.create_task(asyncio.to_thread(admit))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            await pending
            raise
        finally:
            self.runner.wake()

    async def _drawing(self, row, client, command, settings, body):
        version = settings["applied_version"]
        presets = await asyncio.to_thread(self.repository.list_presets, row.service_id)
        aliases = list(dict.fromkeys(alias for preset in presets for alias in preset["lora_keys"]))
        direct = (explicit_prompt(command.prompt, aliases, allow_english=True)
                  or explicit_tag_prompt(command.prompt, aliases))
        initial_mode = await asyncio.to_thread(self.repository.current_mode, row.service_id, command.source, version)
        intent = direct or decode_interpretation(
            json.dumps({"mode": "wish", "lora_keys": []}), command.prompt,
            aliases if command.operation == "workflow_draw" else [])
        kind = "image_edit" if body is not None else "txt2img"
        route_options = ({"workflow_number": command.workflow_number} if command.operation == "workflow_draw"
                         else {"force_default": True})
        prepared = await asyncio.to_thread(self.repository.prepare_route, row.service_id, command.source, intent,
                                           kind, **route_options)
        if prepared.group_mode != initial_mode:
            raise ValueError("drawing_route_changed")
        decision = prepared.decision
        if decision.status != "selected":
            text = ("所选工作流与图片类型不匹配：图生图请附带或回复单张图片，文生图请不带图片。"
                    if decision.status == "kind_mismatch" else
                    "所选工作流或默认图生图不可用，请用 /wf 查看。")
            await self._control(row, client, command, version, "drawing_route", text)
            return True
        positive, negative = intent.positive, intent.negative
        task_id = self._task_id(row.service_id, command)
        seed = int.from_bytes(hashlib.sha256(task_id.bytes).digest()[:8], "big") & ((1 << 63) - 1)
        await self._admit_queued(task_id, (row.service_id, command.source.sender_id),
            lambda: self._admit_with_input(task_id, body,
            lambda fields: self.repository.admit(row.service_id, command.source, command.self_id,
                ENDPOINT_KEY, prepared, positive=positive, negative=negative, seed=seed, **fields,
                reference_message_id=command.reference_message_id,
                wish_text=command.prompt if intent.mode == "wish" else None)))
        return True
