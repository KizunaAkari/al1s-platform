import asyncio
import base64
import hashlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from websockets.asyncio.client import connect

from al1s.adapters.postgres.meme_base import fingerprint
from al1s.bot_ai.contracts import AiEnvelope
from al1s.bot_ai.drawing_inference import split_negative_marker
from al1s.bot_drawing.contracts import PromptIntent
from al1s.meme_workshop.contracts import CommandSource
from .capabilities import compatible_inspection
from .moderation import AuditDecision, MODEL_REVISION, POLICY_VERSION
from .media import _image_format
from .tagger import extract_tags, tag_workflow
from .workflow import instantiate_workflow, managed_graph


def _online_output_format(task):
    original = task.get("wish_text") if task.get("wish_text") is not None else task.get("positive") or ""
    positive, _ = split_negative_marker(original)
    text = unicodedata.normalize("NFKC", positive).casefold()
    text = re.sub(r"(?:不要|不需要|无需|不想要|别|不是|没有|不用|without|no|not|don't|do not)\s*"
                  r"(?:生成|制作|输出|做|有|create|generate|make)?\s*(?:(?:any|an?|the)\s+)?(?:animated\s+)?"
                  r"(?:gif(?:\s*(?:动态图片|动态图|动图))?|动态图片|动态图|动图|视频|video|mp4)", "", text)
    if re.search(r"(?<![a-z0-9])gif(?![a-z0-9])|动态图片|动态图|动图", text):
        return "gif"
    if re.search(r"(?:生成|制作|做|创作|输出|来|变成|一段|一支|一部|一个)[^。.!?？,，;；\n]{0,32}"
                 r"(?:视频|短片)(?!的?(?:封面|截图|海报|插画|静态图|图片|照片))|"
                 r"\b(?:generate|create|make|render|produce)\b[^.!?\n]{0,32}\b(?:video|mp4)\b|"
                 r"\bvideo\s+(?:of|with|showing)\b|\bmp4\b", text):
        return "video"
    return "png"


def _online_error_code(status, body):
    try:
        data = json.loads(body)
    except (ValueError, TypeError, UnicodeError):
        data = {}
    error = data.get("error") if isinstance(data, dict) else None
    error = error if isinstance(error, dict) else {}
    code = error.get("code") if isinstance(error.get("code"), str) else ""
    kind = error.get("type") if isinstance(error.get("type"), str) else ""
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    if code in {"moderation_blocked", "content_policy_violation"}:
        return "drawing_online_moderation_blocked"
    if code in {"insufficient_quota", "billing_hard_limit_reached", "credit_balance_exhausted",
                "project_spend_limit_exceeded", "organization_spend_limit_exceeded",
                "organization_usage_limit_exceeded", "usage_limit_exceeded"}:
        return "drawing_online_quota_exceeded"
    if code in {"rate_limit_exceeded", "slow_down"}:
        return "drawing_online_rate_limited"
    if not code and kind == "insufficient_quota":
        return "drawing_online_quota_exceeded"
    if not code and kind == "rate_limit_exceeded":
        return "drawing_online_rate_limited"
    if code == "model_not_found":
        return "drawing_online_model_unavailable"
    if (code == "organization_not_verified" or (status in {400, 403} and re.match(
            r"\s*(?:your )?organi[sz]ation (?:must|needs? to|has to|is required to) (?:be )?verif",
            message, re.IGNORECASE))):
        return "drawing_online_org_verification"
    return {401: "drawing_online_auth_failed", 403: "drawing_online_access_denied",
            404: "drawing_online_model_unavailable"}.get(status, f"drawing_online_http_{status}")


async def _online_error_response(response):
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in response.aiter_bytes(chunk_size=4096):
                if len(body) + len(chunk) > 16 * 1024:
                    return _online_error_code(response.status_code, b""), None
                body.extend(chunk)
    except (httpx.HTTPError, OSError, TimeoutError):
        return _online_error_code(response.status_code, b""), None
    code = _online_error_code(response.status_code, body)
    if code != "drawing_online_moderation_blocked":
        return code, None
    error = json.loads(body).get("error", {})
    details = error.get("moderation_details") if isinstance(error, dict) else None
    if not isinstance(details, dict):
        return code, None
    stage = details.get("moderation_stage")
    stage = stage if isinstance(stage, str) and stage in {"input", "output", "unknown"} else None
    allowed = {"harassment", "harassment/threatening", "hate", "hate/threatening", "illicit", "illicit/violent",
               "self-harm", "self-harm/intent", "self-harm/instructions", "sexual", "sexual/minors",
               "violence", "violence/graphic", "other"}
    categories = []
    if isinstance(details.get("categories"), list):
        for value in details["categories"]:
            if not isinstance(value, str):
                continue
            category = value if value in allowed else "unrecognized"
            if category not in categories:
                categories.append(category)
    return code, {"stage": stage, "categories": categories} if stage or categories else None


@dataclass(frozen=True)
class PreparedExecution:
    graph: dict
    execution_hash: str
    nodes: list[str]
    assets: list[dict]
    dependency_signature: str | None = None
    output_node: str | None = None
    adapter_version: str | None = None
    node_labels: dict[str, str] | None = None


class DrawingTaskRunner:
    def __init__(self, repository, configuration, media, auditor, sender, client_for_service, *, gpu_queue=None,
                 prompt_model=None):
        self.repository, self.configuration, self.media = repository, configuration, media
        self.auditor, self.sender, self.client_for_service = auditor, sender, client_for_service
        self.gpu_queue = gpu_queue
        self.prompt_model = prompt_model
        self._prompt_tasks = {}
        self._registration_tasks = {}
        self._audit_lock = asyncio.Lock()
        self._progress_tasks, self._progress_data = {}, {}
        self.endpoint_key = "comfy-local-8188"
        self._wake, self._task = asyncio.Event(), None
        self._online_wake, self._online_task, self._online_inflight = asyncio.Event(), None, None
        self._online_task_id = None
        self._online_needs_recovery = True
        self._next_purge = 0.0
        self._next_orphan_scan = 0.0

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        if self._online_task is None and callable(getattr(self.repository, "claim_online_next", None)):
            self._online_task = asyncio.create_task(self._run_online())

    def wake(self):
        self._wake.set()
        self._online_wake.set()

    def progress(self, task_id):
        return self._progress_data.get(str(task_id))

    async def _audit(self, body):
        async with self._audit_lock:
            pending = getattr(self.auditor, "_pending", None)
            if pending is not None and not pending.done():
                try:
                    await asyncio.wait_for(asyncio.shield(pending), timeout=getattr(self.auditor, "timeout", 15))
                except TimeoutError:
                    return AuditDecision("unknown", hashlib.sha256(body).hexdigest())
                except Exception:
                    pass
            return await self.auditor.audit(body)

    async def _watch_progress(self, task, endpoint, owner, graph, ready):
        address = urlsplit(endpoint)
        url = f"ws://{address.netloc}/ws?clientId={quote(owner, safe='')}"
        identity = str(task["id"])
        while True:
            try:
                async with connect(url, proxy=None, open_timeout=3, close_timeout=1,
                                   max_size=2 * 1024 * 1024) as socket:
                    ready.set()
                    async for raw in socket:
                        if not isinstance(raw, str) or len(raw) > 65536:
                            continue
                        try:
                            event = json.loads(raw)
                        except ValueError:
                            continue
                        data = event.get("data") if isinstance(event, dict) else None
                        if not isinstance(data, dict) or data.get("prompt_id") != str(task["prompt_id"]):
                            continue
                        kind, node = event.get("type"), data.get("node")
                        if kind not in {"executing", "progress"}:
                            continue
                        if node is None and kind == "executing":
                            self._progress_data.pop(identity, None)
                            continue
                        if node is None:
                            node = self._progress_data.get(identity, {}).get("node_id")
                        node = str(node) if node is not None else ""
                        if node not in graph:
                            continue
                        entry = {"node_id": node, "node_name": graph[node].get("class_type", ""),
                                 "updated_at": datetime.now(UTC).isoformat(), "value": None, "max": None}
                        value, maximum = data.get("value"), data.get("max")
                        if kind == "progress" and type(value) is int and type(maximum) is int and 0 <= value <= maximum <= 1000000:
                            entry["value"], entry["max"] = value, maximum
                        self._progress_data[identity] = entry
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.getLogger(__name__).warning("drawing_progress_unavailable:%s", type(error).__name__)
            finally:
                ready.set()
                self._progress_data.pop(identity, None)
            await asyncio.sleep(2)

    async def _start_progress(self, task, client, execution=None):
        task_id = task["id"]
        if task_id in self._progress_tasks or not getattr(client, "base_url", None) or not getattr(client, "owner", None):
            return
        try:
            execution = execution or await self._build_execution(task)
        except Exception as error:
            logging.getLogger(__name__).warning("drawing_progress_unavailable:%s", type(error).__name__)
            return
        ready = asyncio.Event()
        labels = {node: {"class_type": (execution.node_labels or {}).get(node, value.get("class_type", ""))}
                  for node, value in execution.graph.items()}
        self._progress_tasks[task_id] = asyncio.create_task(
            self._watch_progress(task, client.base_url, client.owner, labels, ready))
        try:
            await asyncio.wait_for(ready.wait(), timeout=2)
        except TimeoutError:
            pass

    async def _stop_progress(self, task_id):
        watcher = self._progress_tasks.pop(task_id, None)
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        self._progress_data.pop(str(task_id), None)

    async def cancelled(self, task_id):
        pending = self._prompt_tasks.get(task_id)
        if pending is not None:
            pending.cancel()
        registration = self._registration_tasks.get(task_id)
        if registration is not None:
            registration.cancel()
        if self._online_task_id == task_id and self._online_inflight is not None:
            self._online_inflight.cancel()
        if self.gpu_queue is not None:
            task = await asyncio.to_thread(self.repository.task_for_worker, task_id)
            if task["generation_status"] in {"cancelled", "expired", "failed"}:
                await self._stop_progress(task_id)
                if task["cleanup_status"] == "done":
                    self.gpu_queue.finish(str(task_id))
        self.wake()

    async def close(self):
        online = [task for task in (self._online_task, self._online_inflight) if task is not None]
        for task in online:
            task.cancel()
        if online:
            await asyncio.gather(*online, return_exceptions=True)
        self._online_task, self._online_inflight, self._online_task_id = None, None, None
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        pending = (*self._prompt_tasks.values(), *self._registration_tasks.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._prompt_tasks.clear()
        self._registration_tasks.clear()
        for task_id in tuple(self._progress_tasks):
            await self._stop_progress(task_id)
        if self.auditor is not None:
            self.auditor.close()

    def _online_finished(self, task):
        if not task.cancelled():
            try:
                if task.exception() is not None:
                    self._online_needs_recovery = True
            except asyncio.CancelledError:
                pass
        self._online_inflight, self._online_task_id = None, None
        self.wake()

    async def _run_online(self):
        from al1s.adapters.postgres.bot_drawing_repository import ONLINE_ENDPOINT
        while True:
            try:
                if self._online_needs_recovery and self._online_inflight is None:
                    await asyncio.to_thread(self.repository.recover_online)
                    self._online_needs_recovery = False
                await asyncio.to_thread(self.repository.expire_next, ONLINE_ENDPOINT)
                delivery = await asyncio.to_thread(self.repository.next_delivery, ONLINE_ENDPOINT)
                if delivery:
                    await self.deliver(*delivery)
                cleanup = await asyncio.to_thread(self.repository.next_cleanup, ONLINE_ENDPOINT)
                if cleanup and cleanup != self._online_task_id:
                    await self.recover_cleanup(cleanup)
                if self._online_inflight is None:
                    identity = await asyncio.to_thread(self.repository.claim_online_next)
                    if identity is not None:
                        self._online_task_id = identity
                        self._online_inflight = asyncio.create_task(self._execute_online(identity))
                        self._online_inflight.add_done_callback(self._online_finished)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.getLogger(__name__).warning("drawing_online_pending:%s", type(error).__name__)
            self._online_wake.clear()
            try:
                await asyncio.wait_for(self._online_wake.wait(), timeout=2)
            except TimeoutError:
                pass

    async def _execute_online(self, task_id):
        from al1s.bot_ai.configuration import existing_https_proxy
        from .comfy import _image

        sent = False
        try:
            task = await asyncio.to_thread(self.repository.task_for_worker, task_id)
            output_format = _online_output_format(task)
            if output_format == "video":
                await self._fail_unsubmitted(task_id, "drawing_video_unavailable")
                return
            if task["prompt_status"] in {"waiting", "working"}:
                if self.prompt_model is None:
                    await self._fail_unsubmitted(task_id, "drawing_prompt_model_unconfigured")
                    return
                await self._expand_prompt(task_id)
                task = await asyncio.to_thread(self.repository.task_for_worker, task_id)
                if task["generation_status"] != "submitting":
                    return
            if not await asyncio.to_thread(self.repository.online_task_allowed, task_id):
                raise ValueError("drawing_scope_denied")
            task = await self._audit_reference(task)
            if task is None:
                return
            config, secret = await asyncio.to_thread(self.repository.online_credentials,
                task["service_id"], task["preset_id"], task["preset_revision"])
            mode = await self._mode(task, denied_fallback="green")
            prompt = task["positive"] or ""
            if task.get("wish_text"):
                original_positive, _ = split_negative_marker(task["wish_text"])
                prompt += "\nOriginal request: " + original_positive
            if task.get("negative"):
                prompt += "\nAvoid the following: " + task["negative"]
            if mode == "green":
                prompt += "\nCreate only SFW content. Do not include NSFW content."
            params = {"model": config["model_id"], "prompt": prompt,
                      "size": "auto" if output_format == "gif" else config["size"],
                      "quality": config["quality"], "n": 1, "output_format": output_format}
            files = None
            if task.get("input_reference"):
                body = await asyncio.to_thread(self.media.read, task["input_reference"], expected_sha256=task["input_sha256"])
                ext = PurePosixPath(task["input_reference"]).suffix.lstrip(".")
                files = {"image": ("reference." + ext, body, "image/" + ext)}
            proxy = existing_https_proxy() if config["network_mode"] == "proxy" else None
            if not await asyncio.to_thread(self.repository.online_task_allowed, task_id):
                raise ValueError("drawing_scope_denied")
            if not await asyncio.to_thread(self.repository.online_state, task_id, "submitting"):
                return

            async def trace(name, _info):
                nonlocal sent
                if name.endswith("send_request_body.complete") and not sent:
                    sent = await asyncio.to_thread(self.repository.online_state, task_id, "sent")

            endpoint = (config["base_url"] or "https://api.openai.com/v1").rstrip("/")
            endpoint += "/images/edits" if files else "/images/generations"
            timeout = httpx.Timeout(300.0, connect=15.0, write=30.0)
            async with asyncio.timeout(300), httpx.AsyncClient(
                    proxy=proxy, trust_env=False, timeout=timeout, follow_redirects=False) as client:
                kwargs = {"data": {key: str(value) for key, value in params.items()}, "files": files} if files else {"json": params}
                async with client.stream("POST", endpoint, headers={"Authorization": "Bearer " + secret},
                                         extensions={"trace": trace}, **kwargs) as response:
                    if not sent:
                        sent = await asyncio.to_thread(self.repository.online_state, task_id, "sent")
                    if not response.is_success:
                        code, details = await _online_error_response(response)
                        await asyncio.to_thread(self.repository.fail_online, task_id, code, "failed", details)
                        return
                    response.raise_for_status()
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(payload) + len(chunk) > 12 * 1024 * 1024:
                            raise ValueError("drawing_online_output_invalid")
                        payload.extend(chunk)
            data = json.loads(payload)
            images = data.get("data") if isinstance(data, dict) else None
            encoded = images[0].get("b64_json") if isinstance(images, list) and len(images) == 1 and isinstance(images[0], dict) else None
            if not isinstance(encoded, str) or len(encoded) > 12 * 1024 * 1024:
                raise ValueError("drawing_online_output_invalid")
            body = base64.b64decode(encoded, validate=True)
            if output_format == "gif":
                if _image_format(body, allow_gif=True) != "gif":
                    raise ValueError("drawing_online_output_invalid")
            else:
                body = _image(body)
            if not await asyncio.to_thread(self.repository.online_state, task_id, "received"):
                return
            task = await asyncio.to_thread(self.repository.task_for_worker, task_id)
            checksum = hashlib.sha256(body).hexdigest()
            version = await asyncio.to_thread(self.repository.reserve_output, task_id,
                expected_version=task["row_version"], size=len(body), checksum=checksum)
            reference, checksum = await asyncio.to_thread(self.media.put, task_id, "output", body)
            version = await asyncio.to_thread(self.repository.transition, task_id, "submitting", "succeeded",
                expected_version=version, finished_at=datetime.now(UTC), output_reference=reference,
                output_sha256=checksum, output_bytes=len(body))
            version = await asyncio.to_thread(self.repository.set_stage, task_id, "output_audit", expected_version=version)
            decision = await self._audit(body)
            version = await asyncio.to_thread(self.repository.record_audit, task_id, expected_version=version, decision=decision)
            mode = await self._mode(task, denied_fallback="green")
            kind = "image" if mode == "r18" or decision.status == "allow" else "failure_notice"
            version = await asyncio.to_thread(self.repository.set_result_ready, task_id, expected_version=version, reply_kind=kind)
            await asyncio.to_thread(self.repository.mark_cleanup, task_id, expected_version=version, status="done")
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError:
            await asyncio.to_thread(self.repository.fail_online, task_id, "drawing_online_rejected", "failed")
        except (httpx.HTTPError, OSError, TimeoutError):
            await asyncio.to_thread(self.repository.fail_online, task_id, "drawing_online_unknown", "unknown")
        except (ValueError, TypeError, KeyError) as error:
            reason = str(error)
            code = ("drawing_online_proxy_unavailable" if reason in {"ai_proxy_unavailable", "ai_proxy_credentials_unsupported"} else
                    reason if reason in {"drawing_scope_denied", "drawing_media_full", "drawing_online_unconfigured",
                    "drawing_prompt_model_unconfigured", "drawing_online_output_invalid"} else "drawing_online_output_invalid")
            await asyncio.to_thread(self.repository.fail_online, task_id, code, "failed")
        except Exception as error:
            logging.getLogger(__name__).warning("drawing_online_execution_failed:%s", type(error).__name__)
            await asyncio.to_thread(self.repository.fail_online, task_id, "drawing_online_unknown", "unknown")

    async def _run(self):
        while True:
            try:
                if self.gpu_queue is not None and self.gpu_queue.recovering:
                    rows = await asyncio.to_thread(self.repository.gpu_recovery, self.endpoint_key)
                    unresolved = [row for row in rows if row["active"]]
                    queued = [row for row in rows if not row["active"]]
                    first = unresolved[:1]
                    rest = unresolved[1:] + queued
                    self.gpu_queue.restore(active=(str(first[0]["id"]), first[0]["owner"]) if first else None,
                        pending=[(str(row["id"]), row["owner"]) for row in rest])
                expired = await asyncio.to_thread(self.repository.expire_next, self.endpoint_key)
                if expired is not None and self.gpu_queue is not None:
                    row = await asyncio.to_thread(self.repository.task_for_worker, expired)
                    if row["cleanup_status"] == "done":
                        self.gpu_queue.finish(str(expired))
                delivery = await asyncio.to_thread(self.repository.next_delivery, self.endpoint_key)
                if delivery:
                    await self.deliver(*delivery)
                cleanup = await asyncio.to_thread(self.repository.next_cleanup, self.endpoint_key)
                if cleanup:
                    try:
                        await self.recover_cleanup(cleanup)
                    except Exception as error:
                        logging.getLogger(__name__).warning("drawing_cleanup_pending:%s", type(error).__name__)
                if self.gpu_queue is None:
                    registration = await asyncio.to_thread(self.repository.next_registration, self.endpoint_key)
                else:
                    try:
                        expected_registration = UUID(self.gpu_queue.active)
                    except (ValueError, TypeError, AttributeError):
                        expected_registration = None
                    registration = (await asyncio.to_thread(self.repository.next_registration, self.endpoint_key,
                                                              expected_registration)
                                    if expected_registration is not None else None)
                if (registration and registration not in self._registration_tasks and registration not in self._prompt_tasks
                        and (self.gpu_queue is None or self.gpu_queue.active == str(registration))):
                    pending = asyncio.create_task(self._register_one(registration))
                    self._registration_tasks[registration] = pending
                    pending.add_done_callback(lambda done, identity=registration:
                                              self._registration_finished(identity, done))
                active = await asyncio.to_thread(self.repository.next_active, self.endpoint_key)
                try:
                    if active and (self.gpu_queue is None or self.gpu_queue.active == str(active)):
                        await self.advance(active)
                    elif not active:
                        expected = None
                        if self.gpu_queue is not None:
                            try:
                                expected = UUID(self.gpu_queue.active)
                            except (ValueError, TypeError, AttributeError):
                                expected = None
                        claimed = (await asyncio.to_thread(self.repository.claim_next, self.endpoint_key, expected)
                                   if expected is not None else
                                   await asyncio.to_thread(self.repository.claim_next, self.endpoint_key)
                                   if self.gpu_queue is None else None)
                        if claimed:
                            await self.advance(claimed.id)
                        elif expected is not None:
                            row = await asyncio.to_thread(self.repository.task_for_worker, expected)
                            if (row["generation_status"] in {"succeeded", "failed", "cancelled", "expired"}
                                    and row["cleanup_status"] == "done"):
                                self.gpu_queue.finish(str(expected))
                except Exception as error:
                    logging.getLogger(__name__).warning("drawing_execution_pending:%s", type(error).__name__)
                delivery = await asyncio.to_thread(self.repository.next_delivery, self.endpoint_key)
                if delivery:
                    await self.deliver(*delivery)
                deletion = await asyncio.to_thread(self.repository.next_deletion)
                if deletion:
                    try:
                        await asyncio.to_thread(self._delete_history_media, deletion)
                    except (OSError, ValueError) as error:
                        logging.getLogger(__name__).warning("drawing_delete_pending:%s", type(error).__name__)
                        row = await asyncio.to_thread(self.repository.task_for_worker, deletion)
                        if row["deletion_state"] == "deleting":
                            await asyncio.to_thread(self.repository.defer_check, deletion,
                                                    expected_version=row["row_version"], seconds=30)
                media_task = await asyncio.to_thread(self.repository.next_media_cleanup)
                if media_task:
                    await self.cleanup_media(media_task)
                if self.media is not None and time.monotonic() >= self._next_orphan_scan:
                    await asyncio.to_thread(self.repository.cleanup_orphan_media, self.media)
                    self._next_orphan_scan = time.monotonic() + 60
                if time.monotonic() >= self._next_purge:
                    await asyncio.to_thread(self.repository.purge_records, limit=100)
                    self._next_purge = time.monotonic() + 3600
            except Exception as error:
                logging.getLogger(__name__).warning("drawing_worker_pending:%s", type(error).__name__)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2)
            except TimeoutError:
                pass

    async def _fail_unsubmitted(self, task_id, code):
        task = await asyncio.to_thread(self.repository.task_for_worker, task_id)
        if task["generation_status"] not in {"queued", "submitting"} or task["prompt_transport_started"]:
            return
        code = code if re.fullmatch(r"[a-z0-9_]{1,64}", code) else "drawing_processing_failed"
        version = await asyncio.to_thread(self.repository.transition, task_id, task["generation_status"], "failed",
            expected_version=task["row_version"], finished_at=datetime.now(UTC), error_code=code)
        version = await asyncio.to_thread(self.repository.set_result_ready, task_id, expected_version=version,
                                           reply_kind="failure_notice")
        safe = task["resource_registration_state"] == "pending" and task["boot_id"] is None
        await asyncio.to_thread(self.repository.mark_cleanup, task_id, expected_version=version,
                                status="done" if safe else "deferred")
        if safe and self.gpu_queue is not None:
            self.gpu_queue.finish(str(task_id))

    async def cleanup_media(self, task_id):
        task = await asyncio.to_thread(self.repository.task_for_worker, task_id)
        for kind in ("input", "output"):
            if kind == "output" and task.get("output_reference"):
                continue
            size, checksum = task.get(f"{kind}_bytes"), task.get(f"{kind}_sha256")
            if not size or not checksum:
                continue
            reference = task.get(f"{kind}_reference")
            if reference is None and kind == "output":
                extension = "gif" if task.get("endpoint_key") == "openai-images" and _online_output_format(task) == "gif" else "png"
                reference = f"al1s-drawing/{task_id}/output.{extension}"
            if not reference:
                raise ValueError("drawing_media_reference_invalid")
            await asyncio.to_thread(self.media.delete_if_present, reference, expected_sha256=checksum)
            await asyncio.to_thread(self.repository.clear_media, task_id, kind, checksum=checksum, size=size)
        await asyncio.to_thread(self.media.remove_empty, task_id)

    def _delete_history_media(self, task_id):
        with self.media.staging():
            task = self.repository.task_for_worker(task_id)
            if task["deletion_state"] == "deleted":
                return
            if task["deletion_state"] != "deleting":
                raise ValueError("drawing_delete_pending")
            for kind in ("input", "output"):
                size, checksum = task.get(f"{kind}_bytes"), task.get(f"{kind}_sha256")
                if not size and not checksum and not task.get(f"{kind}_reference"):
                    continue
                if not size or not checksum:
                    raise ValueError("drawing_media_reference_invalid")
                reference = task.get(f"{kind}_reference")
                if reference is None and kind == "output":
                    extension = "gif" if task.get("endpoint_key") == "openai-images" and _online_output_format(task) == "gif" else "png"
                    reference = f"al1s-drawing/{task_id}/output.{extension}"
                if not reference:
                    raise ValueError("drawing_media_reference_invalid")
                self.media.delete_if_present(reference, expected_sha256=checksum)
                self.repository.clear_deleted_media(task_id, kind, checksum=checksum, size=size)
            self.media.remove_empty(task_id)
            self.repository.finish_delete_task(task_id)

    async def delete_history(self, task_ids):
        deleted, pending = [], []
        for task_id in task_ids:
            try:
                await asyncio.to_thread(self._delete_history_media, task_id)
                deleted.append(str(task_id))
            except (OSError, ValueError):
                pending.append(str(task_id))
                row = await asyncio.to_thread(self.repository.task_for_worker, task_id)
                if row["deletion_state"] == "deleting":
                    await asyncio.to_thread(self.repository.defer_check, task_id,
                                            expected_version=row["row_version"], seconds=30)
        self.wake()
        return {"deleted": deleted, "pending": pending}

    async def _build_execution(self, task) -> PreparedExecution:
        image_data = None
        if task["input_reference"]:
            body = await asyncio.to_thread(self.media.read, task["input_reference"], expected_sha256=task["input_sha256"])
            extension = PurePosixPath(task["input_reference"]).suffix.lstrip(".")
            mime = {"png": "png", "jpeg": "jpeg", "webp": "webp"}.get(extension)
            if mime is None:
                raise ValueError("drawing_image_invalid")
            image_data = f"data:image/{mime};base64," + base64.b64encode(body).decode("ascii")
        if task["operation"] == "tag":
            if image_data is None:
                raise ValueError("drawing_image_required")
            graph = tag_workflow(image_data)
            managed = managed_graph(graph, task["prompt_id"])
            return PreparedExecution(managed.graph, managed.graph_hash,
                                     sorted({item["class_type"] for item in graph.values()}), [],
                                     output_node=managed.output.node_id,
                                     node_labels={key: value["class_type"] for key, value in graph.items()})
        workflow = await asyncio.to_thread(self.repository.get_workflow, task["service_id"], task["preset_id"],
                                           task["preset_revision"])
        dependencies = await asyncio.to_thread(self.repository.get_dependencies, task["service_id"], task["preset_id"],
            task["preset_revision"], for_preview=task["origin"] == "admin_preview")
        signature = fingerprint({key: dependencies[key] for key in ("adapter", "nodes", "assets")})
        if signature != task["dependency_signature"]:
            raise ValueError("drawing_dependency_changed")
        execution = instantiate_workflow(workflow, task["positive"], task["negative"] or "",
                                         image_data=image_data, seed=task["seed"], prompt_id=task["prompt_id"],
                                         mode=task.get("audit_mode") or "green")
        managed = managed_graph(execution.graph, task["prompt_id"])
        return PreparedExecution(managed.graph, managed.graph_hash,
            sorted({item["class_type"] for item in execution.graph.values()}), dependencies["assets"], signature,
            managed.output.node_id, dependencies["adapter"],
            {key: value["class_type"] for key, value in workflow.source_graph.items()})

    async def _expand_prompt(self, task_id):
        try:
            profile_id, revision = await asyncio.to_thread(self.repository.begin_prompt_expansion, task_id)
            task = await asyncio.to_thread(self.repository.task_for_worker, task_id)
            intent = PromptIntent(mode="wish", negative=task.get("negative") or "",
                                  lora_keys=tuple(task.get("requested_lora_keys") or ()))
            positive_text, marked_negative = split_negative_marker(task["wish_text"])
            user_text = positive_text if marked_negative and marked_negative in intent.negative else task["wish_text"]
            positive, negative = await self.prompt_model.expand_drawing(
                task["service_id"], profile_id, revision, user_text, intent,
                workflow_name=task["prompt_workflow_name"], aliases=task.get("prompt_aliases") or [])
            await asyncio.to_thread(self.repository.finish_prompt_expansion,
                                    task_id, profile_id, revision, positive, negative)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = str(error) if isinstance(error, ValueError) else "drawing_processing_failed"
            await self._fail_unsubmitted(task_id, code)

    def _prompt_finished(self, task_id, task):
        self._prompt_tasks.pop(task_id, None)
        if not task.cancelled():
            task.exception()
        self.wake()

    async def _register_one(self, task_id):
        try:
            await self.register(task_id)
        except asyncio.CancelledError:
            raise
        except ValueError as error:
            if str(error) != "drawing_task_conflict":
                await self._fail_unsubmitted(task_id, str(error))
        except Exception as error:
            logging.getLogger(__name__).warning("drawing_registration_pending:%s", type(error).__name__)

    def _registration_finished(self, task_id, task):
        self._registration_tasks.pop(task_id, None)
        if not task.cancelled():
            task.exception()
        self.wake()

    async def register(self, task_id):
        task = await asyncio.to_thread(self.repository.task_for_worker, task_id)
        if task["generation_status"] != "queued" or task["resource_registration_state"] == "confirmed":
            return "unchanged"
        if self.gpu_queue is not None and self.gpu_queue.active != str(task_id):
            return "waiting_turn"
        if task.get("prompt_status") in {"waiting", "working"}:
            if self.prompt_model is None:
                await self._fail_unsubmitted(task_id, "drawing_prompt_model_unconfigured")
                return "failed"
            if task_id not in self._prompt_tasks:
                pending = asyncio.create_task(self._expand_prompt(task_id))
                self._prompt_tasks[task_id] = pending
                pending.add_done_callback(lambda done: self._prompt_finished(task_id, done))
            return "prompting"
        task = await self._audit_reference(task)
        if task is None:
            return "blocked"
        if task.get("processing_stage") != "preparing_resources":
            version = await asyncio.to_thread(self.repository.set_stage, task_id, "preparing_resources",
                                              expected_version=task["row_version"])
            task = {**task, "row_version": version, "processing_stage": "preparing_resources"}
        local_ready = getattr(self.configuration, "local_settings_ready", None)
        if local_ready is not None and not await local_ready(task["service_id"]):
            await asyncio.to_thread(self.repository.defer_check, task_id, expected_version=task["row_version"], seconds=5)
            return "waiting_for_resources"
        if not await self.configuration.vision_ready(task["service_id"]):
            await asyncio.to_thread(self.repository.defer_check, task_id, expected_version=task["row_version"], seconds=5)
            return "waiting_for_vision"
        client = (await self.configuration.client(task["service_id"], endpoint=task["comfy_endpoint"])
                  if task.get("comfy_endpoint") else await self.configuration.client(task["service_id"]))
        try:
            state = await client.state()
            boot = state["boot_id"]
            if task["boot_id"] is not None and task["boot_id"] != boot:
                try:
                    await asyncio.to_thread(self.repository.reset_unsubmitted_after_restart, task_id,
                        expected_version=task["row_version"], boot_id=task["boot_id"],
                        endpoint=task.get("comfy_endpoint"))
                    return "queued"
                except ValueError as error:
                    if str(error) != "drawing_task_conflict":
                        raise
                await asyncio.to_thread(self.repository.defer_check, task_id,
                                        expected_version=task["row_version"], seconds=5)
                return "unknown"
            execution = await self._build_execution(task)
            if task["execution_hash"] is not None and task["execution_hash"] != execution.execution_hash:
                raise ValueError("drawing_execution_changed")
            names = [{"kind": item["kind"], "name": item["name"]} for item in execution.assets]
            inspected = await client.inspect(execution.nodes, names)
            if inspected.get("boot_id") != boot or inspected.get("adapter") != state.get("adapter"):
                raise ValueError("drawing_dependency_changed")
            if execution.dependency_signature is not None:
                compatible = compatible_inspection(inspected, execution.adapter_version, execution.nodes)
                signature = fingerprint({key: compatible[key] for key in ("adapter", "nodes", "assets")})
                if signature != execution.dependency_signature:
                    raise ValueError("drawing_dependency_changed")
            version = await asyncio.to_thread(self.repository.registration_unknown, task_id,
                expected_version=task["row_version"], boot_id=boot, execution_hash=execution.execution_hash)
            try:
                result = await client.register(task["prompt_id"], task["service_id"], task["operation"],
                                               execution.execution_hash, boot, inspected["assets"])
            except (httpx.HTTPError, OSError, TimeoutError, ValueError):
                return "unknown"
            if result.get("state") != "registered":
                return "unknown"
            await asyncio.to_thread(self.repository.mark_registration, task_id,
                expected_version=version, boot_id=boot, execution_hash=execution.execution_hash)
            return "confirmed"
        finally:
            await client.close()

    async def advance(self, task_id):
        task = await asyncio.to_thread(self.repository.task_for_worker, task_id)
        if task["generation_status"] == "submitting" and not task["prompt_transport_started"]:
            if task.get("expires_at") is not None and datetime.now(UTC) >= task["expires_at"]:
                await self._fail_unsubmitted(task_id, "drawing_queue_expired")
                return "expired"
            task = await self._audit_reference(task)
            if task is None:
                return "blocked"
        client = (await self.configuration.client(task["service_id"], endpoint=task["comfy_endpoint"])
                  if task.get("comfy_endpoint") else await self.configuration.client(task["service_id"]))
        try:
            state = await client.state()
            if state.get("boot_id") != task["boot_id"]:
                await self._stop_progress(task_id)
                if (task["generation_status"] == "submitting" and not task["prompt_transport_started"]
                        and task["boot_id"]):
                    try:
                        await asyncio.to_thread(self.repository.reset_unsubmitted_after_restart, task_id,
                            expected_version=task["row_version"], boot_id=task["boot_id"],
                            endpoint=task.get("comfy_endpoint"))
                        return "queued"
                    except ValueError as error:
                        if str(error) != "drawing_task_conflict":
                            raise
                if (task["generation_status"] == "cancelling"
                        and task["prompt_transport_started"]
                        and task["boot_id"] and task["drawing_version"] is not None):
                    settings = await asyncio.to_thread(
                        self.repository.get_settings, task["service_id"]
                    )
                    if (task.get("comfy_endpoint") or
                            (settings["row_version"] == task["drawing_version"]
                             and settings["applied_version"] == task["drawing_version"])):
                        native = await client._json(
                            "GET", f'/api/jobs/{task["prompt_id"]}', missing=True
                        )
                        if native is None:
                            queue = await client._json("GET", "/queue")
                            running = queue.get("queue_running")
                            pending = queue.get("queue_pending")
                            if (isinstance(running, list) and isinstance(pending, list)
                                    and not running and not pending):
                                await asyncio.to_thread(
                                    self.repository.finish_restart_cancel, task_id,
                                    expected_version=task["row_version"],
                                    drawing_version=task["drawing_version"],
                                    endpoint=task.get("comfy_endpoint"),
                                )
                                if self.gpu_queue is not None:
                                    self.gpu_queue.finish(str(task_id))
                                return "cancelled"
                await asyncio.to_thread(self.repository.defer_check, task_id, expected_version=task["row_version"], seconds=5)
                return "unknown"
            if task["generation_status"] == "submitting" and not task["prompt_transport_started"]:
                if self.gpu_queue is not None:
                    profile = (f'{task["preset_id"]}:{task["preset_revision"]}'
                               if task["operation"] == "draw" else "tag")
                    if await client.switch("draw", profile) != "ready":
                        await asyncio.to_thread(self.repository.defer_check, task_id,
                                                expected_version=task["row_version"], seconds=2)
                        return "waiting_for_resources"
                execution = await self._build_execution(task)
                if execution.execution_hash != task["execution_hash"]:
                    raise ValueError("drawing_execution_changed")
                await self._start_progress(task, client, execution)
                version = await asyncio.to_thread(self.repository.begin_submit, task_id, expected_version=task["row_version"])
                try:
                    receipt = await client.submit(task["prompt_id"], task["service_id"], task["operation"],
                                                  task["execution_hash"], execution.graph)
                except (httpx.HTTPError, OSError, TimeoutError, ValueError):
                    receipt = "unknown"
                if receipt == "rejected":
                    await self._stop_progress(task_id)
                    version = await asyncio.to_thread(self.repository.transition, task_id, "submitting", "failed",
                        expected_version=version, finished_at=datetime.now(UTC), error_code="drawing_prompt_rejected")
                    version = await asyncio.to_thread(self.repository.set_result_ready, task_id,
                                                       expected_version=version, reply_kind="failure_notice")
                    try:
                        rejected = await client.reject(task["prompt_id"], task["service_id"], task["operation"],
                                                       task["execution_hash"])
                    except (httpx.HTTPError, OSError, TimeoutError, ValueError):
                        rejected = False
                    if rejected:
                        await self._release(task, client, version)
                    else:
                        await asyncio.to_thread(self.repository.mark_cleanup, task_id, expected_version=version,
                                                status="deferred")
                    return "failed"
                target = "running" if receipt == "accepted" else "reconciling"
                await asyncio.to_thread(self.repository.transition, task_id, "submitting", target, expected_version=version)
                return target
            if task["generation_status"] == "submitting":
                version = await asyncio.to_thread(self.repository.transition, task_id, "submitting", "reconciling",
                                                  expected_version=task["row_version"])
                task = {**task, "generation_status": "reconciling", "row_version": version}
            if task["generation_status"] in {"running", "reconciling", "cancelling"}:
                await self._start_progress(task, client)
            if task["generation_status"] == "cancelling":
                outcome = await client.cancel(task["prompt_id"], task["service_id"], task["operation"], task["execution_hash"])
                if outcome == "cancelled":
                    await self._stop_progress(task_id)
                    version = await asyncio.to_thread(self.repository.transition, task_id, "cancelling", "cancelled",
                        expected_version=task["row_version"], finished_at=datetime.now(UTC), error_code="drawing_cancelled")
                    version = await asyncio.to_thread(self.repository.set_result_ready, task_id,
                                                       expected_version=version, reply_kind="failure_notice")
                    await self._release(task, client, version)
                    return "cancelled"
                if outcome == "unknown":
                    version = await asyncio.to_thread(self.repository.transition, task_id, "cancelling", "reconciling",
                                                      expected_version=task["row_version"])
                    await asyncio.to_thread(self.repository.defer_check, task_id, expected_version=version, seconds=5)
                    return "unknown"
            result = await client.query(task["prompt_id"], task["service_id"], task["operation"], task["execution_hash"])
            native_status = result.get("status") if result is not None else None
            if native_status in {None, "pending", "in_progress"}:
                now, version = datetime.now(UTC), task["row_version"]
                started = task.get("started_at")
                if native_status == "in_progress" and started is None:
                    version = await asyncio.to_thread(self.repository.mark_native_started, task_id,
                                                      expected_version=version)
                    started = now
                expired = (native_status == "pending" and task.get("expires_at") is not None
                           and now >= task["expires_at"])
                timed_out = native_status != "pending" and started is not None and now - started >= timedelta(seconds=300)
                if task["generation_status"] in {"running", "reconciling"} and (expired or timed_out):
                    await asyncio.to_thread(self.repository.transition, task_id, task["generation_status"], "cancelling",
                                            expected_version=version)
                    return "cancelling"
                await asyncio.to_thread(self.repository.defer_check, task_id, expected_version=version)
                return "pending"
            if result.get("status") in {"completed", "failed", "cancelled"}:
                await self._stop_progress(task_id)
                execution = await self._build_execution(task)
                await self.complete(task_id, result, client, output_node=execution.output_node)
                return result.get("status")
            await asyncio.to_thread(self.repository.defer_check, task_id, expected_version=task["row_version"], seconds=5)
            return "unknown"
        finally:
            await client.close()

    async def _mode(self, task, *, denied_fallback=None):
        if task["origin"] == "admin_preview":
            return task.get("audit_mode") or "green"
        source = CommandSource(message_id=task["source_message_id"], scope_kind=task["scope_kind"],
                               target_id=task["target_id"], sender_id=task["sender_id"])
        try:
            return await asyncio.to_thread(self.repository.current_mode, task["service_id"], source, task["drawing_version"])
        except ValueError:
            if denied_fallback is None:
                raise
            return denied_fallback

    async def _audit_reference(self, task):
        mode = await self._mode(task)
        if task["operation"] == "draw" and task.get("audit_mode") != "green" and mode == "green":
            await self._fail_unsubmitted(task["id"], "drawing_mode_changed_retry")
            return None
        if not task.get("input_reference"):
            return task
        valid = self._input_audit_valid(task, {"allow", "block"})
        if not valid:
            body = await asyncio.to_thread(self.media.read, task["input_reference"], expected_sha256=task["input_sha256"])
            version = await asyncio.to_thread(self.repository.set_stage, task["id"], "input_audit",
                                              expected_version=task["row_version"])
            task = {**task, "row_version": version, "processing_stage": "input_audit"}
            try:
                decision = await self._audit(body)
            except Exception:
                decision = AuditDecision("unknown", task["input_sha256"])
            version = await asyncio.to_thread(self.repository.record_input_audit, task["id"],
                                              expected_version=task["row_version"], decision=decision)
            task = {**task, "row_version": version, "input_audit_status": decision.status,
                    "input_audit_sha256": decision.image_sha256, "input_audit_model_revision": decision.model_revision,
                    "input_audit_policy_version": decision.policy_version}
        if await self._mode(task) == "green" and task["input_audit_status"] != "allow":
            code = "drawing_input_blocked" if task["input_audit_status"] == "block" else "drawing_audit_unknown"
            version = await asyncio.to_thread(self.repository.transition, task["id"], task["generation_status"], "failed",
                expected_version=task["row_version"], finished_at=datetime.now(UTC), error_code=code)
            version = await asyncio.to_thread(self.repository.set_result_ready, task["id"],
                                               expected_version=version, reply_kind="failure_notice")
            known_unregistered = task["resource_registration_state"] == "pending" and task.get("boot_id") is None
            await asyncio.to_thread(self.repository.mark_cleanup, task["id"], expected_version=version,
                                    status="done" if known_unregistered else "deferred")
            if known_unregistered and self.gpu_queue is not None:
                self.gpu_queue.finish(str(task["id"]))
            return None
        return task

    @staticmethod
    def _input_audit_valid(task, statuses):
        return (bool(task.get("input_sha256")) and task.get("input_audit_status") in statuses
                and task.get("input_audit_sha256") == task.get("input_sha256")
                and task.get("input_audit_model_revision") == MODEL_REVISION
                and task.get("input_audit_policy_version") == POLICY_VERSION)

    async def _release(self, task, client, version, *, pending_result=False):
        try:
            deadline = time.monotonic() + (3 if pending_result else 0)
            while True:
                state = await client.release(task["prompt_id"], task["service_id"], task["operation"], task["execution_hash"])
                if state != "deferred" or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.1)
            cleaned = state in {"released", "retained"} and await client.delete_history(
                task["prompt_id"], task["service_id"], task["operation"], task["execution_hash"])
        except (httpx.HTTPError, OSError, TimeoutError, ValueError):
            cleaned = False
        cleanup = "pending" if cleaned and pending_result else "done" if cleaned else "deferred"
        version = await asyncio.to_thread(self.repository.mark_cleanup, task["id"], expected_version=version,
                                          status=cleanup)
        if cleaned and self.gpu_queue is not None:
            self.gpu_queue.finish(str(task["id"]))
        return version, cleaned

    async def recover_cleanup(self, task_id):
        task = await asyncio.to_thread(self.repository.task_for_worker, task_id)
        version = task["row_version"]
        audit = getattr(self.auditor, "_pending", None)
        if task.get("processing_stage") == "waiting_audit_stop" and audit is not None and not audit.done():
            await asyncio.to_thread(self.repository.defer_check, task_id, expected_version=version, seconds=2)
            return
        if task["generation_status"] == "succeeded" and not task["reply_kind"] and not task.get("cancel_requested"):
            version = await self._recover_result(task, version)
        elif not task["reply_kind"]:
            version = await asyncio.to_thread(self.repository.set_result_ready, task_id,
                                               expected_version=version, reply_kind="failure_notice")
        if task["resource_registration_state"] == "pending" and task["boot_id"] is None:
            await asyncio.to_thread(self.repository.mark_cleanup, task_id, expected_version=version, status="done")
            if self.gpu_queue is not None:
                self.gpu_queue.finish(str(task_id))
            return
        client = (await self.configuration.client(task["service_id"], endpoint=task["comfy_endpoint"])
                  if task.get("comfy_endpoint") else await self.configuration.client(task["service_id"]))
        try:
            state = await client.state()
            if state.get("boot_id") != task["boot_id"]:
                if isinstance(state.get("boot_id"), str) and state["boot_id"] and not task["prompt_transport_started"]:
                    await asyncio.to_thread(self.repository.mark_cleanup, task_id, expected_version=version, status="done")
                    if self.gpu_queue is not None:
                        self.gpu_queue.finish(str(task_id))
                    return
                await asyncio.to_thread(self.repository.defer_check, task_id, expected_version=version, seconds=5)
                return
            if task.get("error_code") == "drawing_prompt_rejected":
                if not await client.reject(task["prompt_id"], task["service_id"], task["operation"],
                                           task["execution_hash"]):
                    await asyncio.to_thread(self.repository.defer_check, task_id, expected_version=version, seconds=5)
                    return
            if task["resource_registration_state"] == "unknown" and not task["prompt_transport_started"]:
                execution = await self._build_execution(task)
                inspected = await client.inspect(execution.nodes, [{"kind": item["kind"], "name": item["name"]}
                                                                  for item in execution.assets])
                await client.register(task["prompt_id"], task["service_id"], task["operation"],
                                      task["execution_hash"], task["boot_id"], inspected["assets"])
            version, cleaned = await self._release(task, client, version)
            if cleaned and task["origin"] == "admin_preview":
                fresh = await asyncio.to_thread(self.repository.task_for_worker, task_id)
                if fresh["reply_kind"] == "image":
                    peak = (await client.state()).get("peak_allocated_bytes")
                    if type(peak) is int and peak >= 0:
                        await asyncio.to_thread(self.repository.mark_preview_validated, task_id, measured_peak_bytes=peak)
        finally:
            await client.close()

    async def _recover_result(self, task, version):
        mode = await self._mode(task, denied_fallback="green")
        if task["operation"] == "tag":
            if mode == "green" and not self._input_audit_valid(task, {"allow"}):
                body = await asyncio.to_thread(self.media.read, task["input_reference"], expected_sha256=task["input_sha256"])
                version = await asyncio.to_thread(self.repository.set_stage, task["id"], "input_audit",
                                                   expected_version=version)
                decision = await self._audit(body)
                version = await asyncio.to_thread(self.repository.record_input_audit, task["id"],
                                                   expected_version=version, decision=decision)
                allowed = decision.status == "allow"
            else:
                allowed = mode == "r18" or self._input_audit_valid(task, {"allow"})
            kind = "tag_text" if allowed else "failure_notice"
        else:
            body = await asyncio.to_thread(self.media.read, task["output_reference"], expected_sha256=task["output_sha256"])
            version = await asyncio.to_thread(self.repository.set_stage, task["id"], "output_audit",
                                               expected_version=version)
            decision = await self._audit(body)
            version = await asyncio.to_thread(self.repository.record_audit, task["id"], expected_version=version, decision=decision)
            kind = "image" if mode == "r18" or decision.status == "allow" else "failure_notice"
        return await asyncio.to_thread(self.repository.set_result_ready, task["id"], expected_version=version, reply_kind=kind)

    async def deliver(self, task_id, kind):
        task = await asyncio.to_thread(self.repository.task_for_worker, task_id)
        client = self.client_for_service(task["service_id"])
        if client is None or self.sender is None or task["origin"] != "qq":
            return
        source = CommandSource(message_id=task["source_message_id"], scope_kind=task["scope_kind"],
                               target_id=task["target_id"], sender_id=task["sender_id"])
        envelope = AiEnvelope(request_id=task_id, service_id=task["service_id"], source=source,
            self_id=task["bot_self_id"], sender_name="群友", parts=[{"type": "text", "text": "绘图任务"}],
            config_version=task["drawing_version"], deadline=datetime.now(UTC) + timedelta(seconds=30))
        guard, root = None, None
        if kind == "notice":
            lines = ["当前任务队列"]
            queue = self.gpu_queue
            if queue is None or queue.recovering:
                lines.extend(["本地（共享 GPU）", "状态暂不可用"])
            else:
                active = queue.active is not None
                total = int(active) + queue.pending
                position = queue.position(str(task_id))
                own = position if active or position is None else position - 1
                if total:
                    lines.append("本地（共享 GPU）")
                for index in range(total):
                    state = "处理中" if active and index == 0 else "等待中"
                    lines.append(f"{index + 1}. {state}" + ("（本次）" if index == own else ""))
            try:
                positions = await asyncio.to_thread(self.repository.online_positions)
            except Exception:
                positions = None
            if positions is None:
                lines.extend(["线上绘图", "状态暂不可用"])
            elif positions:
                lines.append("线上绘图")
                for identity, index in sorted(positions.items(), key=lambda item: item[1]):
                    state = ("状态待核对" if self._online_needs_recovery else
                             "处理中" if identity == str(self._online_task_id) else "等待中")
                    lines.append(f"{index + 1}. {state}" + ("（本次）" if identity == str(task_id) else ""))
            if len(lines) == 1:
                return
            text = "\n".join(lines)
            if task.get("route_reason") == "lora_unmatched":
                text += " 未匹配的 LoRA 已忽略，使用默认工作流。"
            parts = [{"type": "text", "text": text}]
        elif task["reply_kind"] == "image":
            reference = self.media.root / task["output_reference"]
            root = self.media.root / "al1s-drawing"
            parts = [{"type": "image", "media_ref": str(reference)}]
            guard = lambda _ref, digest: digest == task["output_sha256"]
        elif task["reply_kind"] == "tag_text":
            parts = [{"type": "text", "text": "画面标签（根据图片推测）：\n" + task["tag_result"]}]
        else:
            messages = {"drawing_input_blocked": "参考图未通过本群的绿色模式审核。",
                "drawing_audit_blocked": "生成结果未通过本群的绿色模式审核。",
                "drawing_audit_unknown": "图片审核暂不可用或无法确认，本次未发送图片。",
                "drawing_negative_unsupported": "绿色模式需要工作流配置负面提示词入口，请联系管理员。",
                "drawing_mode_changed_retry": "本群已切换为绿色模式，请重新发送绘图请求。",
                "drawing_prompt_model_unconfigured": "请先在 AI 绘图通用设置中选择许愿提示词模型。",
                "drawing_llm_timeout": "绘图提示词模型响应超时，请稍后重试。",
                "drawing_llm_provider_error": "绘图提示词模型请求失败，请检查模型配置或网络后重试。",
                "drawing_wish_invalid": "整理绘图描述失败，请换一种说法后重试。",
                "drawing_online_rejected": "线上绘图服务拒绝请求，请联系管理员检查配置。",
                "drawing_online_moderation_blocked": "线上绘图未通过服务商内容审核，请检查描述或参考图。",
                "drawing_online_auth_failed": "线上绘图 API 认证失败，请联系管理员检查密钥。",
                "drawing_online_quota_exceeded": "线上绘图服务额度或预算不足，请联系管理员检查账单和限额。",
                "drawing_online_rate_limited": "线上绘图请求过于频繁，请稍后重试。",
                "drawing_online_model_unavailable": "线上绘图模型不可用，请联系管理员检查模型及权限。",
                "drawing_online_access_denied": "线上绘图服务拒绝访问，请联系管理员检查权限。",
                "drawing_online_org_verification": "线上绘图需要完成组织验证，请联系管理员检查。",
                "drawing_online_unknown": "线上绘图结果暂时无法确认，请联系管理员。",
                "drawing_online_output_invalid": "线上绘图服务返回的图片无法使用，请联系管理员检查。",
                "drawing_online_unconfigured": "线上绘图尚未配置密钥，请联系管理员。",
                "drawing_online_proxy_unavailable": "线上绘图代理不可用，请联系管理员检查网络。",
                "drawing_video_unavailable": "视频生成暂未接入；可以请求 GIF 动图。",
                "drawing_cancelled": "绘图任务已取消。",
                "drawing_media_full": "绘图存储空间已满，请联系管理员清理历史任务。",
                "drawing_queue_expired": "绘图请求排队超时，请稍后重新发送。",
                "drawing_queue_full": "绘图队列已满，请稍后重试。"}
            fallback = ("线上绘图服务拒绝请求，请联系管理员检查配置。" if (task["error_code"] or "").startswith("drawing_online_")
                        else "绘图任务未完成，请检查工作流和服务状态。")
            error_code = task["error_code"] or ""
            status = re.fullmatch(r"drawing_online_http_([1-5]\d{2})", error_code)
            if status:
                number = status.group(1)
                direction = {"400": "检查请求内容和模型配置", "402": "检查账单和付款状态",
                             "429": "检查额度或请求频率"}.get(number, "检查请求或服务状态")
                fallback = f"线上绘图服务返回 HTTP {number}，请联系管理员{direction}。"
            if error_code == "drawing_online_moderation_blocked":
                details = task.get("online_moderation_details") or {}
                if details.get("stage") == "input":
                    messages[error_code] = "线上绘图未通过服务商输入审核，请检查描述或参考图。"
                elif details.get("stage") == "output":
                    messages[error_code] = "线上绘图结果未通过服务商审核，请调整描述或参考图。"
            parts = [{"type": "text", "text": messages.get(error_code, fallback)}]
        result = await self.sender.send(envelope, 0, parts, client, receipt_repository=self.repository,
            receipt_kind=kind, media_root=root, prepared_image_guard=guard)
        if kind == "result" and result.get("code") == "audit_recheck_required":
            await self._recheck_delivery(task_id)

    async def _recheck_delivery(self, task_id):
        task = await asyncio.to_thread(self.repository.task_for_worker, task_id)
        try:
            mode = await self._mode(task)
        except ValueError:
            return
        if mode != "green" or task["reply_kind"] not in {"image", "tag_text"}:
            return
        reference = task["output_reference"] if task["reply_kind"] == "image" else task["input_reference"]
        checksum = task["output_sha256"] if task["reply_kind"] == "image" else task["input_sha256"]
        body = await asyncio.to_thread(self.media.read, reference, expected_sha256=checksum)
        decision = await self._audit(body)
        method = self.repository.record_audit if task["reply_kind"] == "image" else self.repository.record_input_audit
        await asyncio.to_thread(method, task_id, expected_version=task["row_version"], decision=decision)
        await asyncio.to_thread(self.repository.requeue_result, task_id,
                                task["reply_kind"] if decision.status == "allow" else "failure_notice")

    async def complete(self, task_id, result, client, *, output_node):
        task = await asyncio.to_thread(self.repository.task_for_worker, task_id)
        status = result.get("status")
        if task.get("cancel_requested") and status in {"completed", "failed", "cancelled"}:
            version = await asyncio.to_thread(self.repository.transition, task_id, task["generation_status"], "cancelled",
                expected_version=task["row_version"], finished_at=datetime.now(UTC), error_code="drawing_cancelled")
            version = await asyncio.to_thread(self.repository.set_result_ready, task_id,
                                               expected_version=version, reply_kind="failure_notice")
            await self._release(task, client, version)
            return
        if status in {"failed", "cancelled"}:
            target = "cancelled" if status == "cancelled" else "failed"
            version = await asyncio.to_thread(self.repository.transition, task_id, task["generation_status"], target,
                expected_version=task["row_version"], finished_at=datetime.now(UTC), error_code="drawing_generation_failed")
            version = await asyncio.to_thread(self.repository.set_result_ready, task_id, expected_version=version,
                                               reply_kind="failure_notice")
            await self._release(task, client, version)
            return
        if status != "completed":
            raise ValueError("drawing_task_not_terminal")
        kind, released, cleaned = None, False, False
        try:
            if task["operation"] == "tag":
                tags = extract_tags(result, prompt_id=task["prompt_id"], owner=client.owner)
                version = await asyncio.to_thread(self.repository.transition, task_id, task["generation_status"],
                    "succeeded", expected_version=task["row_version"], finished_at=datetime.now(UTC), tag_result=tags)
                version, cleaned = await self._release(task, client, version, pending_result=True)
                released = True
                mode = await self._mode(task, denied_fallback="green")
                valid_input = self._input_audit_valid(task, {"allow"})
                if mode == "green" and not valid_input:
                    body = await asyncio.to_thread(self.media.read, task["input_reference"], expected_sha256=task["input_sha256"])
                    version = await asyncio.to_thread(self.repository.set_stage, task_id, "input_audit",
                                                       expected_version=version)
                    decision = await self._audit(body)
                    version = await asyncio.to_thread(self.repository.record_input_audit, task_id,
                                                       expected_version=version, decision=decision)
                    allowed = decision.status == "allow"
                else:
                    allowed = mode == "r18" or valid_input
                kind = "tag_text" if allowed else "failure_notice"
            else:
                body = await client.download(task["prompt_id"], task["service_id"], task["operation"],
                                             task["execution_hash"], output_node)
                checksum = hashlib.sha256(body).hexdigest()
                version = await asyncio.to_thread(self.repository.reserve_output, task_id,
                    expected_version=task["row_version"], size=len(body), checksum=checksum)
                reference, checksum = await asyncio.to_thread(self.media.put, task_id, "output", body)
                version = await asyncio.to_thread(self.repository.transition, task_id, task["generation_status"],
                    "succeeded", expected_version=version, finished_at=datetime.now(UTC),
                    output_reference=reference, output_sha256=checksum, output_bytes=len(body))
                version, cleaned = await self._release(task, client, version, pending_result=True)
                released = True
                version = await asyncio.to_thread(self.repository.set_stage, task_id, "output_audit",
                                                   expected_version=version)
                decision = await self._audit(body)
                version = await asyncio.to_thread(self.repository.record_audit, task_id,
                                                   expected_version=version, decision=decision)
                mode = await self._mode(task, denied_fallback="green")
                kind = "image" if mode == "r18" or decision.status == "allow" else "failure_notice"
            version = await asyncio.to_thread(self.repository.set_result_ready, task_id,
                                               expected_version=version, reply_kind=kind)
            if cleaned:
                version = await asyncio.to_thread(self.repository.mark_cleanup, task_id,
                                                  expected_version=version, status="done")
        except (httpx.HTTPError, OSError, TimeoutError, ValueError) as error:
            fresh = await asyncio.to_thread(self.repository.task_for_worker, task_id)
            if fresh.get("cancel_requested") and fresh["generation_status"] in {"running", "reconciling", "cancelling"}:
                version = await asyncio.to_thread(self.repository.transition, task_id, fresh["generation_status"],
                    "cancelled", expected_version=fresh["row_version"], finished_at=datetime.now(UTC),
                    error_code="drawing_cancelled")
                version = await asyncio.to_thread(self.repository.set_result_ready, task_id,
                                                   expected_version=version, reply_kind="failure_notice")
                await self._release(task, client, version)
                return
            if fresh["generation_status"] != "succeeded":
                version = await asyncio.to_thread(self.repository.transition, task_id, fresh["generation_status"],
                    "failed", expected_version=fresh["row_version"], finished_at=datetime.now(UTC),
                    error_code="drawing_media_full" if str(error) == "drawing_media_full" else "drawing_output_invalid")
                version = await asyncio.to_thread(self.repository.set_result_ready, task_id,
                                                   expected_version=version, reply_kind="failure_notice")
            else:
                if not released:
                    await asyncio.to_thread(self.repository.mark_cleanup, task_id,
                                            expected_version=fresh["row_version"], status="deferred")
                return
        if not released:
            version, cleaned = await self._release(task, client, version)
        if cleaned and task["origin"] == "admin_preview" and kind == "image":
            state = await client.state()
            peak = state.get("peak_allocated_bytes")
            if type(peak) is int and peak >= 0:
                await asyncio.to_thread(self.repository.mark_preview_validated, task_id, measured_peak_bytes=peak)
