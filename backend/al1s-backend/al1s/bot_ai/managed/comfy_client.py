import asyncio
import contextlib
import json
import re
import time
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

import httpx
from al1s.bot_ai.history import VisionJob as _Job
from al1s.bot_ai.gpu_queue import gpu_owner
from .smart_contract import local_endpoint

_LOCKS = WeakValueDictionary()
MODEL = "Qwen/Qwen3-VL-4B-Instruct"


def vision_workflow(prompt: str, image_data: str | None, max_tokens: int) -> dict:
    inputs = {"model_name": "Qwen3-VL-4B-Instruct", "quantization": "4-bit (VRAM-friendly)",
        "attention_mode": "sdpa", "use_torch_compile": False, "device": "auto",
        "preset_prompt": "🖼️ Detailed Description", "custom_prompt": prompt[:24000],
        "max_tokens": min(1024, max(64, max_tokens)), "temperature": 0.1, "top_p": 0.9,
        "num_beams": 1, "repetition_penalty": 1.1, "frame_count": 9,
        "video_frame_size": "auto", "keep_model_loaded": True, "seed": 1}
    graph = {"2": {"class_type": "AILab_QwenVL_Advanced", "inputs": inputs},
             "3": {"class_type": "ShowText|pysssss", "inputs": {"text": ["2", 0]}}}
    if image_data is not None:
        if not image_data.startswith("data:image/") or len(image_data) > 12 * 1024 * 1024:
            raise ValueError("comfy_image_invalid")
        graph["1"] = {"class_type": "ImageReceiver", "inputs": {"image": "#DATA",
            "link_id": 0, "save_to_workflow": True, "image_data": image_data,
            "trigger_always": False}}
        inputs["image"] = ["1", 0]
    return graph


def managed_vision_workflow(prompt: str, image_data: str | None, max_tokens: int, prompt_id: UUID):
    from al1s.bot_drawing.media import MAX_IMAGE_DATA
    from al1s.bot_drawing.workflow import MAX_GRAPH_BYTES, _hash

    graph = vision_workflow(prompt, image_data, max_tokens)
    graph["2"]["class_type"] = "AL1SQwenVLAdvanced"
    graph["2"]["inputs"]["al1s_prompt_id"] = str(prompt_id)
    if "1" in graph:
        graph["1"]["class_type"] = "AL1SImageReceiver"
        graph["1"]["inputs"]["al1s_prompt_id"] = str(prompt_id)
    return graph, _hash(graph, limit=MAX_GRAPH_BYTES + MAX_IMAGE_DATA)


class ComfyWorkflowClient:
    def __init__(self, base_url, owner, load_pending, save_pending, *, client=None, timeout=90,
                 resource=None, service_id=None, config_version=None, gpu_queue=None):
        self.base_url, self.owner = local_endpoint(base_url), owner
        self.load_pending, self.save_pending = load_pending, save_pending
        self.http = client or httpx.AsyncClient(base_url=self.base_url + "/", trust_env=False, timeout=15)
        self.timeout = timeout
        if resource is not None and (not isinstance(service_id, UUID) or type(config_version) is not int
                                     or config_version < 1):
            raise ValueError("comfy_resource_configuration_invalid")
        self.resource, self.service_id, self.config_version = resource, service_id, config_version
        self._lock = None
        self._cleaner = None
        self._closed = False
        self.gpu_queue, self._gpu_lease = gpu_queue, None
        self._gpu_cleanup_ready = False

    def _mutex(self):
        if self._lock is None:
            key = (asyncio.get_running_loop(), self.base_url, self.owner)
            lock = _LOCKS.get(key)
            if lock is None:
                lock = asyncio.Lock()
                _LOCKS[key] = lock
            self._lock = lock
        return self._lock

    async def _json(self, method, path, body=None, *, missing=False):
        async with self.http.stream(method, path, json=body) as response:
            if missing and response.status_code == 404:
                return None
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 16 * 1024 * 1024:
                    raise ValueError("comfy_response_too_large")
        result = json.loads(data) if data else {}
        if not isinstance(result, dict):
            raise ValueError("comfy_response_invalid")
        return result

    async def _jobs(self):
        rows = await self.load_pending()
        if not isinstance(rows, list) or len(rows) > 16:
            raise ValueError("comfy_cleanup_invalid")
        try:
            return [_Job.model_validate(row) for row in rows]
        except ValueError:
            raise ValueError("comfy_cleanup_invalid") from None

    async def _save(self, jobs):
        await self.save_pending([job.model_dump(mode="json", exclude_none=True, exclude_defaults=True) for job in jobs])

    async def _get_job(self, job):
        identity = str(job.prompt_id)
        if job.boot_id is not None:
            if self.resource is None:
                raise ValueError("comfy_resource_unavailable")
            result = await self.resource.query(job.prompt_id, self.service_id, "vision", job.execution_hash)
        else:
            result = await self._json("GET", "/api/jobs/" + identity, missing=True)
        if result is not None and result.get("id") != identity:
            raise ValueError("comfy_job_id_mismatch")
        if result is not None and result.get("status") in {"completed", "failed", "cancelled"}:
            owner = result.get("workflow", {}).get("extra_data", {}).get("client_id")
            if owner != self.owner:
                raise ValueError("comfy_job_owner_mismatch")
        return result

    async def _previous_boot_finished(self, job):
        identity = str(job.prompt_id)
        result = await self._json("GET", "/api/jobs/" + identity, missing=True)
        if result is None:
            queue = await self._json("GET", "/queue")
            running, waiting = queue.get("queue_running"), queue.get("queue_pending")
            if not isinstance(running, list) or not isinstance(waiting, list):
                raise ValueError("comfy_queue_invalid")
            return not running and not waiting
        if result.get("id") != identity:
            raise ValueError("comfy_job_id_mismatch")
        if result.get("status") not in {"completed", "failed", "cancelled"}:
            return False
        workflow = result.get("workflow")
        extra = workflow.get("extra_data") if isinstance(workflow, dict) else None
        owner = extra.get("client_id") if isinstance(extra, dict) else None
        if owner != self.owner:
            raise ValueError("comfy_job_owner_mismatch")
        await self._json("POST", "/history", {"delete": [identity]})
        return True

    async def _reap(self):
        jobs = await self._jobs()
        remaining = []
        current_boot = None
        for job in jobs:
            if job.boot_id is not None and (not job.transport_started or job.registration_state == 'rejected'):
                if not await self._release_unsubmitted(job):
                    remaining.append(job)
                continue
            if job.boot_id is not None:
                if self.resource is None:
                    raise ValueError("comfy_resource_unavailable")
                if current_boot is None:
                    current_boot = (await self.resource.state()).get("boot_id")
                    if not isinstance(current_boot, str) or not current_boot:
                        raise ValueError("comfy_resource_unsupported")
                if current_boot != job.boot_id:
                    if not await self._previous_boot_finished(job):
                        remaining.append(job)
                    continue
            state = await self._get_job(job)
            if state is None:
                inspect = getattr(self.resource, "job_state", None) if job.boot_id is not None else None
                if inspect is not None:
                    registration = await inspect(job.prompt_id, self.service_id, "vision", job.execution_hash)
                    if registration.get("state") == "dequeued":
                        released = await self.resource.release(job.prompt_id, self.service_id, "vision", job.execution_hash)
                        if released in {"released", "retained"}:
                            continue
                if job.boot_id is not None or time.time() - job.created_at < 60:
                    remaining.append(job)
            elif state.get("status") in {"completed", "failed", "cancelled"}:
                if job.boot_id is not None:
                    released = await self.resource.release(job.prompt_id, self.service_id, "vision", job.execution_hash)
                    if released not in {"released", "retained"}:
                        remaining.append(job)
                        continue
                await self._json("POST", "/history", {"delete": [str(job.prompt_id)]})
            else:
                if job.boot_id is None:
                    await self._json("POST", f"/api/jobs/{job.prompt_id}/cancel")
                    remaining.append(job)
                else:
                    result = await self.resource.cancel(job.prompt_id, self.service_id, "vision", job.execution_hash)
                    if result == "cancelled":
                        released = await self.resource.release(job.prompt_id, self.service_id, "vision", job.execution_hash)
                        if released not in {"released", "retained"}:
                            remaining.append(job)
                    else:
                        remaining.append(job)
        if len(remaining) != len(jobs):
            await self._save(remaining)
        return remaining

    async def _release_unsubmitted(self, job):
        if self.resource is None:
            raise ValueError('comfy_resource_unavailable')
        state = await self.resource.state()
        if state.get('boot_id') != job.boot_id:
            return True
        if job.registration_state == 'rejected':
            if not await self.resource.reject(job.prompt_id, self.service_id, 'vision', job.execution_hash):
                return False
        else:
            registration = await self.resource.register(job.prompt_id, self.service_id, 'vision',
                                                        job.execution_hash, job.boot_id, [])
            if registration.get('state') == 'released':
                return True
            if registration.get('state') not in {'registered', 'dequeued'}:
                return False
        status = await self.resource.release(job.prompt_id, self.service_id, 'vision', job.execution_hash)
        return status in {'released', 'retained'}

    async def cleanup(self):
        async with self._mutex():
            remaining = await self._reap()
            self._finish_gpu(remaining)
            return remaining

    def _finish_gpu(self, remaining):
        if (not remaining and self.gpu_queue is not None and self._gpu_lease is not None
                and self._gpu_cleanup_ready):
            self.gpu_queue.finish(self._gpu_lease)
            self._gpu_lease = None
            self._gpu_cleanup_ready = False

    def start_cleanup(self):
        if not self._closed and (self._cleaner is None or self._cleaner.done()):
            self._cleaner = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        while not self._closed:
            await asyncio.sleep(2)
            try:
                if not await self.cleanup():
                    return
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(28)

    async def _check_version(self):
        stats = await self._json("GET", "/system_stats")
        match = re.match(r"^(\d+)\.(\d+)", str(stats.get("system", {}).get("comfyui_version", "")))
        if not match or tuple(map(int, match.groups())) < (0, 43):
            raise ValueError("comfy_version_unsupported")

    async def _wait_result(self, job):
        queue_deadline = time.monotonic() + 1800 if self.gpu_queue is not None else None
        execution_deadline = None
        while True:
            result = await self._get_job(job)
            if result is not None:
                if (result.get("status") == "pending" and self.gpu_queue is not None and self._gpu_lease
                        and self.gpu_queue.vision_cancel_requested(self._gpu_lease)):
                    outcome = await self.resource.cancel_queued(job.prompt_id, self.service_id, "vision", job.execution_hash)
                    if outcome == "cancelled":
                        released = await self.resource.release(job.prompt_id, self.service_id, "vision", job.execution_hash)
                        if released in {"released", "retained"}:
                            await self._save([row for row in await self._jobs() if row.prompt_id != job.prompt_id])
                        raise ValueError("gpu_vision_disabled")
                if result.get("status") == "in_progress" and execution_deadline is None:
                    execution_deadline = time.monotonic() + self.timeout
                if result.get("status") in {"failed", "cancelled"}:
                    raise ValueError("comfy_workflow_failed")
                if result.get("status") == "completed":
                    text = result.get("outputs", {}).get("3", {}).get("text", [])
                    if not isinstance(text, list) or not text or any(not isinstance(s, str) for s in text):
                        raise ValueError("comfy_output_invalid")
                    value = "\n".join(text).strip()
                    if not value or len(value) > 24000:
                        raise ValueError("comfy_output_invalid")
                    return value
            if self.gpu_queue is not None and time.monotonic() >= (execution_deadline or queue_deadline):
                raise TimeoutError
            await asyncio.sleep(0.25)

    async def generate(self, prompt, *, image_data=None, max_tokens=512):
        if self.gpu_queue is None:
            return await self._generate(prompt, image_data=image_data, max_tokens=max_tokens)
        if self.resource is None:
            raise ValueError("comfy_resource_unavailable")
        ticket = "vision:" + str(uuid4())
        self.gpu_queue.reserve(ticket, owner=gpu_owner.get())
        await self.gpu_queue.wait(ticket)
        self._gpu_lease = ticket
        self._gpu_cleanup_ready = False
        try:
            async with asyncio.timeout(1800):
                while True:
                    self.gpu_queue.check_vision(ticket)
                    if await self.resource.switch("vision", None) == "ready":
                        break
                    await asyncio.sleep(1)
            self.gpu_queue.check_vision(ticket)
            return await self._generate(prompt, image_data=image_data, max_tokens=max_tokens)
        finally:
            if self._gpu_lease == ticket:
                self._gpu_cleanup_ready = True
            with contextlib.suppress(httpx.HTTPError, ValueError, TimeoutError):
                async with asyncio.timeout(3):
                    await self.cleanup()
            if self._gpu_lease == ticket:
                self.start_cleanup()

    async def _generate(self, prompt, *, image_data=None, max_tokens=512):
        graph = vision_workflow(prompt, image_data, max_tokens)
        async with self._mutex():
            try:
                async with asyncio.timeout(self.timeout if self.gpu_queue is None else None):
                    await self._check_version()
                    jobs = await self._reap()
                    if self.gpu_queue is not None and jobs:
                        raise ValueError("comfy_cleanup_pending")
                    if len(jobs) >= 16:
                        raise ValueError("comfy_cleanup_full")
                    if self.resource is not None:
                        return await self._generate_managed(jobs, prompt, image_data, max_tokens)
                    job = _Job(prompt_id=uuid4(), created_at=time.time())
                    await self._save([*jobs, job])
                    payload = {"prompt_id": str(job.prompt_id), "client_id": self.owner,
                               "prompt": graph}
                    try:
                        receipt = await self._json("POST", "/prompt", payload)
                    except httpx.TransportError:
                        receipt = None
                    if receipt is not None and receipt.get("prompt_id") != str(job.prompt_id):
                        raise ValueError("comfy_job_id_mismatch")
                    return await self._wait_result(job)
            except TimeoutError:
                raise ValueError("comfy_timeout") from None
            finally:
                with contextlib.suppress(httpx.HTTPError, ValueError, TimeoutError):
                    async with asyncio.timeout(3):
                        await self._reap()
                self.start_cleanup()

    async def _generate_managed(self, jobs, prompt, image_data, max_tokens):
        state = await self.resource.state()
        boot = state.get("boot_id")
        if not isinstance(boot, str) or not boot:
            raise ValueError("comfy_resource_unavailable")
        await self.resource.set_pin(self.service_id, self.config_version, True)
        prompt_id = uuid4()
        graph, digest = managed_vision_workflow(prompt, image_data, max_tokens, prompt_id)
        job = _Job(prompt_id=prompt_id, created_at=time.time(), boot_id=boot,
                   execution_hash=digest, registration_state="pending")
        await self._save([*jobs, job])
        try:
            registered = await self.resource.register(prompt_id, self.service_id, "vision", digest, boot, [])
        except (httpx.HTTPError, ValueError):
            await self._save([*jobs, job.model_copy(update={"registration_state": "unknown"})])
            raise
        if registered.get("state") != "registered":
            raise ValueError("comfy_resource_unavailable")
        job = job.model_copy(update={"registration_state": "confirmed", "transport_started": True})
        await self._save([*jobs, job])
        receipt = await self.resource.submit(prompt_id, self.service_id, "vision", digest, graph)
        if receipt == "rejected":
            job = job.model_copy(update={"registration_state": "rejected"})
            await self._save([*jobs, job])
            raise ValueError("comfy_workflow_failed")
        return await self._wait_result(job)

    async def close(self):
        self._closed = True
        if self._cleaner is not None:
            self._cleaner.cancel()
            await asyncio.gather(self._cleaner, return_exceptions=True)
        await self.http.aclose()
        close_resource = getattr(self.resource, "close", None)
        if close_resource is not None:
            await close_resource()
