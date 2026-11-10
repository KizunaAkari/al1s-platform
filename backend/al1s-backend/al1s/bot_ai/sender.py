import asyncio
import hashlib
import os
import stat
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from al1s.bot_ai.contracts import AiEnvelope
from al1s.bot_ai.media import audio_duration
from al1s.meme_workshop.images import MAX_BYTES, decode_image


class AiSender:
    def __init__(self, repository, media_root: Path, *, interval: float = 1,
                 queue_timeout: float = 10, send_timeout: float = 6, probe=audio_duration) -> None:
        self.repository, self.media_root = repository, Path(media_root).resolve()
        self.interval, self.queue_timeout, self.probe = interval, queue_timeout, probe
        self.send_timeout = send_timeout
        self._inflight: dict[object, asyncio.Task] = {}
        self._locks = defaultdict(asyncio.Lock)
        self._next_send: dict[object, float] = {}
        self._slots = asyncio.Semaphore(16)

    def _read_media(self, request_id, reference: str, kind: str, media_root: Path | None = None) -> bytes:
        root = Path(media_root).resolve() if media_root is not None else self.media_root
        directory = root / str(request_id)
        if directory.is_symlink() or directory.resolve().parent != root:
            raise ValueError("ai_output_directory_invalid")
        path = Path(reference).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_file():
            raise ValueError("ai_output_path_invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError("ai_output_media_invalid")
            body = file.read(MAX_BYTES + 1)
        if not body or len(body) > MAX_BYTES:
            raise ValueError("ai_output_media_invalid")
        if kind == "image":
            decode_image(body)
        elif not 0 < self.probe(path) <= 60:
            raise ValueError("ai_output_audio_duration")
        return body

    def _prepare(self, request: AiEnvelope, parts: list[dict], media_root: Path | None = None) -> list[dict]:
        if sum(p.get("type") == "image" for p in parts) > 1 or sum(
                p.get("type") == "record" for p in parts) > 1:
            raise ValueError("ai_output_count_invalid")
        result, text_count, audio_failed = [], 0, False
        for part in parts:
            kind = part.get("type")
            if kind == "text" and isinstance(part.get("text"), str):
                text = part["text"]
                remaining = max(0, 3000 - text_count)
                if len(text) > remaining:
                    suffix = "…（内容已截断）"
                    text = text[:max(0, remaining - len(suffix))] + (suffix if remaining else "")
                text_count += len(text)
                for index in range(0, len(text), 1000):
                    result.append({"type": "text", "text": text[index:index + 1000]})
            elif kind in {"image", "record"} and isinstance(part.get("media_ref"), str):
                try:
                    body = self._read_media(request.request_id, part["media_ref"], kind, media_root)
                except (OSError, ValueError):
                    if kind != "record":
                        raise
                    audio_failed = True
                    continue
                result.append({"type": kind, "body": body, "media_ref": part["media_ref"]})
            else:
                raise ValueError("ai_output_part_invalid")
        if audio_failed:
            texts = [part for part in result if part["type"] == "text"]
            if not texts:
                fallback = next((part.get("fallback_text") for part in parts
                    if part.get("type") == "record" and isinstance(part.get("fallback_text"), str)), "")[:3000]
                texts = [{"type": "text", "text": fallback[index:index + 1000]} for index in range(0, len(fallback), 1000)]
                result.extend(texts)
                text_count = len(fallback)
            if not texts:
                raise ValueError("ai_output_audio_unavailable")
            note = "\n（语音不可用，已保留文字回复。）"
            spare = 3000 - text_count
            target = next(part for part in reversed(texts) if len(part["text"]) + spare >= len(note))
            limit = min(1000, len(target["text"]) + spare)
            target["text"] = target["text"][:limit - len(note)] + note
        if not result or len(result) > 5:
            raise ValueError("ai_output_count_invalid")
        return result

    async def send(self, request: AiEnvelope, sequence: int, parts: list[dict], client, *,
                   is_active: Callable[[], bool] | None = None,
                   image_allowed: Callable[[str], bool] | None = None,
                   fallback_text: str = "", receipt_repository=None, receipt_kind: str | None = None,
                   media_root: Path | None = None, prepared_image_guard=None) -> dict:
        if receipt_repository is not None:
            return await self._send_receipted(request, sequence, parts, client, receipt_repository,
                receipt_kind, media_root, is_active, prepared_image_guard)
        if sequence != 0 or self._slots.locked():
            return {"status": "busy", "code": "ai_output_busy"}
        async with self._slots:
            try:
                prepared = await asyncio.to_thread(self._prepare, request, parts)
            except (ValueError, OSError):
                return {"status": "failed", "code": "ai_output_invalid"}
            if not await asyncio.to_thread(self.repository.allowed, request.service_id,
                                           request.source, request.config_version):
                return {"status": "denied", "code": "ai_scope_revoked"}
            if is_active is not None and not is_active():
                return {"status": "denied", "code": "ai_request_cancelled"}
            if not await asyncio.to_thread(self.repository.start_send, request.request_id):
                return {"status": "unknown", "code": "ai_send_already_started"}
            status, sent_count, queue_busy, sent_media = "unknown", 0, False, []
            lock = self._locks[request.service_id]
            acquired = False
            try:
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=self.queue_timeout)
                    acquired = True
                except TimeoutError:
                    status, queue_busy = "failed", True
                if acquired:
                    loop = asyncio.get_running_loop()
                    for part in prepared:
                        delay = max(0, self._next_send.get(request.service_id, 0) - loop.time())
                        remaining = (request.deadline - datetime.now(UTC)).total_seconds()
                        if delay + 6 > min(remaining, 10):
                            status = "failed"
                            break
                        if delay:
                            await asyncio.sleep(delay)
                        if not await asyncio.to_thread(self.repository.allowed, request.service_id,
                                                       request.source, request.config_version):
                            status = "failed"
                            break
                        if is_active is not None and not is_active():
                            status = "failed"
                            break
                        self._next_send[request.service_id] = loop.time() + self.interval
                        def send_if_active():
                            if is_active is not None and not is_active():
                                return False
                            selected = part
                            if part['type'] == 'image' and image_allowed is not None:
                                try:
                                    allowed = image_allowed(part['media_ref'])
                                except Exception:
                                    allowed = False
                                if not allowed:
                                    if len(prepared) == 1 and fallback_text:
                                        selected = {'type': 'text', 'text': fallback_text[:3000]}
                                    else:
                                        return 'skipped'
                            if is_active is not None and not is_active():
                                return False
                            client.send_segments(request.source, [selected], quote=sent_count == 0)
                            return 'fallback' if selected is not part else True
                        send_task = asyncio.create_task(asyncio.to_thread(send_if_active))
                        self._inflight[request.request_id] = send_task
                        send_task.add_done_callback(lambda done: self._finish_inflight(request.request_id, done))
                        outcome = await asyncio.wait_for(asyncio.shield(send_task), timeout=self.send_timeout)
                        if outcome is False:
                            status = "failed"
                            break
                        if outcome != 'skipped':
                            sent_count += 1
                            if outcome is True and part['type'] == 'image':
                                sent_media.append(part['media_ref'])
                    else:
                        status = "sent" if sent_count else "failed"
            except (TimeoutError, OSError, ValueError):
                status = "unknown"
            finally:
                if acquired:
                    lock.release()
                await asyncio.shield(asyncio.to_thread(self.repository.finish_send,
                                                      request.request_id, status, sent_count))
            return {"status": "busy" if queue_busy else status, "sent_count": sent_count,
                    "sent_media": sent_media}

    async def _send_receipted(self, request, sequence, parts, client, repository, kind, media_root,
                              is_active, image_guard):
        if sequence != 0 or kind not in {"notice", "result"} or self._slots.locked():
            return {"status": "busy", "code": "ai_output_busy"}
        async with self._slots:
            try:
                prepared = await asyncio.to_thread(self._prepare, request, parts, media_root)
            except (ValueError, OSError):
                return {"status": "failed", "code": "ai_output_invalid"}
            if not await asyncio.to_thread(repository.allowed, request.service_id, request.source, request.config_version):
                return {"status": "denied", "code": "ai_scope_revoked"}
            if is_active is not None and not is_active():
                return {"status": "denied", "code": "ai_request_cancelled"}
            if not await asyncio.to_thread(repository.start_send, request.request_id, kind):
                return {"status": "unknown", "code": "ai_send_already_started"}
            status, code, count, task, receipt = "failed", None, 0, None, None
            lock, acquired = self._locks[request.service_id], False
            try:
                await asyncio.wait_for(lock.acquire(), timeout=self.queue_timeout)
                acquired = True
                delay = max(0, self._next_send.get(request.service_id, 0) - asyncio.get_running_loop().time())
                remaining = (request.deadline - datetime.now(UTC)).total_seconds()
                if delay + self.send_timeout > min(remaining, self.queue_timeout):
                    return {"status": "failed", "code": "ai_output_expired"}
                if delay:
                    await asyncio.sleep(delay)
                if not await asyncio.to_thread(repository.allowed, request.service_id, request.source, request.config_version):
                    return {"status": "denied", "code": "ai_scope_revoked"}

                def send_if_safe():
                    if is_active is not None and not is_active():
                        return "cancelled"
                    image = next((part for part in prepared if part["type"] == "image"), None)
                    digest = hashlib.sha256(image["body"]).hexdigest() if image else None
                    if image is not None and image_guard is not None:
                        try:
                            if not image_guard(image["media_ref"], digest):
                                return "recheck"
                        except Exception:
                            return "recheck"
                    if not repository.mark_transport_started(request.request_id, kind, digest):
                        return "recheck"
                    delivered = client.send_segments(request.source, prepared, quote=kind != "notice")
                    if not isinstance(delivered, (str, int)) or isinstance(delivered, bool) or not str(delivered):
                        raise ValueError("onebot_receipt_unknown")
                    return "sent", str(delivered)

                self._next_send[request.service_id] = asyncio.get_running_loop().time() + self.interval
                task = asyncio.create_task(asyncio.to_thread(send_if_safe))
                key = (request.request_id, kind)
                self._inflight[key] = task
                task.add_done_callback(lambda done: self._finish_inflight(key, done))
                result = await asyncio.wait_for(asyncio.shield(task), timeout=self.send_timeout)
                if isinstance(result, tuple) and result[0] == "sent":
                    status, count, receipt = "sent", 1, result[1]
                elif result == "recheck":
                    code = "audit_recheck_required"
            except asyncio.CancelledError:
                status = "unknown" if task is not None else "failed"
                raise
            except TimeoutError:
                status = "unknown" if task is not None else "failed"
            except Exception:
                status = "unknown" if task is not None else "failed"
            finally:
                if acquired:
                    if task is not None and not task.done():
                        task.add_done_callback(lambda _done: lock.release())
                    else:
                        lock.release()
                await asyncio.shield(asyncio.to_thread(repository.finish_send, request.request_id, kind, status, count, receipt))
            result = {"status": status, "sent_count": count,
                      "sent_media": [part["media_ref"] for part in prepared if count and part["type"] == "image"]}
            if code:
                result["code"] = code
            return result

    def _finish_inflight(self, request_id, task: asyncio.Task) -> None:
        if self._inflight.get(request_id) is task:
            self._inflight.pop(request_id, None)
        if not task.cancelled():
            task.exception()

    def pending_send(self, request_id):
        task = self._inflight.get(request_id)
        if task is not None and not task.done():
            return task
        return next((item for key, item in self._inflight.items()
                     if isinstance(key, tuple) and key[0] == request_id and not item.done()), None)
