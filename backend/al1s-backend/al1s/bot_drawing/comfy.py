import asyncio
import json
import re
from io import BytesIO
from uuid import UUID
from urllib.parse import quote

import httpx
from PIL import Image

from al1s.bot_ai.managed.smart_contract import local_endpoint
from al1s.meme_workshop.images import MAX_BYTES, MAX_PIXELS
from .media import MAX_IMAGE_DATA
from .workflow import MAX_GRAPH_BYTES, _hash

_RESOURCE = "/al1s/resources/v1"


def owned_output(result: object, prompt_id: UUID, node_id: str, *, owner: str | None = None) -> str:
    if not isinstance(result, dict) or result.get("id") != str(prompt_id) or result.get("status") != "completed":
        raise ValueError("comfy_output_invalid")
    if owner is not None:
        workflow = result.get("workflow")
        extra = workflow.get("extra_data") if isinstance(workflow, dict) else None
        if not isinstance(extra, dict) or extra.get("client_id") != owner:
            raise ValueError("comfy_output_invalid")
    outputs = result.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != {node_id}:
        raise ValueError("comfy_output_invalid")
    node = outputs[node_id]
    images = node.get("images") if isinstance(node, dict) else None
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise ValueError("comfy_output_invalid")
    item = images[0]
    filename = item.get("filename")
    if (not isinstance(filename, str) or not re.fullmatch(r"output_[0-9]{5,}_[.]png", filename)
            or item.get("subfolder") not in (f"al1s-drawing/{prompt_id}", f"al1s-drawing\\{prompt_id}")
            or item.get("type") != "output"):
        raise ValueError("comfy_output_invalid")
    return filename


def _image(body: bytes) -> bytes:
    if not body or len(body) > MAX_BYTES:
        raise ValueError("comfy_output_invalid")
    try:
        with Image.open(BytesIO(body), formats=["PNG"]) as image:
            width, height = image.size
            if (image.format != "PNG" or getattr(image, "n_frames", 1) != 1 or min(width, height) < 1
                    or max(width, height) > 4096 or width * height > MAX_PIXELS
                    or image.mode not in {"L", "LA", "RGB", "RGBA"}):
                raise ValueError("comfy_output_invalid")
            image.load()
            clean = Image.frombytes(image.mode, image.size, image.tobytes())
            output = BytesIO()
            clean.save(output, format="PNG", compress_level=4)
            result = output.getvalue()
            if len(result) > MAX_BYTES:
                raise ValueError("comfy_output_invalid")
            return result
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError("comfy_output_invalid") from error


class DrawingComfyClient:
    def __init__(self, base_url: str, owner: str, token: str, *, client=None):
        self.base_url = local_endpoint(base_url)
        if not isinstance(owner, str) or not owner or len(owner) > 128 or not isinstance(token, str) or not token:
            raise ValueError("comfy_resource_configuration_invalid")
        self.owner, self._token = owner, token
        self.http = client or httpx.AsyncClient(base_url=self.base_url + "/", trust_env=False, timeout=15)

    async def _request(self, method, path, *, body=None, params=None, resource=False,
                       limit=16 * 1024 * 1024, missing=False, timeout=None):
        headers = {"Authorization": "Bearer " + self._token} if resource else None
        options = {"timeout": timeout} if timeout is not None else {}
        async with self.http.stream(method, path, json=body, params=params, headers=headers,
                                    follow_redirects=False, **options) as response:
            if missing and response.status_code == 404:
                return None
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError("comfy_response_too_large")
        return bytes(data)

    async def _json(self, method, path, *, body=None, resource=False, missing=False, timeout=None):
        data = await self._request(method, path, body=body, resource=resource, missing=missing, timeout=timeout)
        if data is None:
            return None
        try:
            value = json.loads(data) if data else {}
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("comfy_response_invalid") from error
        if not isinstance(value, dict):
            raise ValueError("comfy_response_invalid")
        return value

    async def _registration(self, prompt_id: UUID, service_id: UUID, operation: str, execution_hash: str):
        if (not isinstance(prompt_id, UUID) or not isinstance(service_id, UUID)
                or operation not in {"draw", "tag", "vision"} or not isinstance(execution_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", execution_hash)):
            raise ValueError("comfy_request_invalid")
        result = await self._json("GET", f"{_RESOURCE}/jobs/{prompt_id}", resource=True, missing=True)
        if (not result or result.get("prompt_id") != str(prompt_id) or result.get("service_id") != str(service_id)
                or result.get("owner") != self.owner or result.get("operation") != operation
                or result.get("execution_hash") != execution_hash):
            raise ValueError("comfy_resource_owner_mismatch")
        return result

    async def state(self) -> dict:
        state = await self._json("GET", f"{_RESOURCE}/state", resource=True)
        if (type(state.get("protocol")) is not int or state["protocol"] != 1
                or not isinstance(state.get("adapter"), str) or not state["adapter"]
                or not isinstance(state.get("boot_id"), str) or not state["boot_id"]):
            raise ValueError("comfy_resource_unsupported")
        return state

    async def inspect(self, nodes: list[str], assets: list[dict]) -> dict:
        if (not isinstance(nodes, list) or len(nodes) > 100 or any(not isinstance(item, str) for item in nodes)
                or not isinstance(assets, list) or len(assets) > 100):
            raise ValueError("comfy_request_invalid")
        result = await self._json("POST", f"{_RESOURCE}/inspect", resource=True, timeout=110,
                                  body={"nodes": nodes, "assets": assets})
        if not isinstance(result.get("assets"), list) or not isinstance(result.get("adapter"), str):
            raise ValueError("comfy_response_invalid")
        return result

    async def node_info(self, names: list[str]) -> dict:
        if (not isinstance(names, list) or len(names) > 100
                or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.|+-]{1,80}", name)
                       for name in names)):
            raise ValueError("comfy_request_invalid")
        semaphore = asyncio.Semaphore(8)
        async def fetch(name):
            async with semaphore:
                result = await self._json("GET", "/object_info/" + quote(name, safe=""))
                if not isinstance(result.get(name), dict):
                    raise ValueError("comfy_node_missing")
                return name, result[name]
        return dict(await asyncio.gather(*(fetch(name) for name in dict.fromkeys(names))))

    async def register(self, prompt_id: UUID, service_id: UUID, operation: str, execution_hash: str,
                       boot_id: str, assets: list[dict]) -> dict:
        if (not isinstance(prompt_id, UUID) or not isinstance(service_id, UUID)
                or operation not in {"draw", "tag", "vision"} or not isinstance(execution_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", execution_hash)
                or not isinstance(boot_id, str) or not boot_id or not isinstance(assets, list)
                or len(assets) > 100):
            raise ValueError("comfy_request_invalid")
        result = await self._json("PUT", f"{_RESOURCE}/jobs/{prompt_id}", resource=True, timeout=110,
            body={"service_id": str(service_id), "owner": self.owner, "operation": operation,
                  "execution_hash": execution_hash, "boot_id": boot_id, "assets": assets})
        if (result.get("prompt_id") != str(prompt_id) or result.get("service_id") != str(service_id)
                or result.get("owner") != self.owner or result.get("operation") != operation
                or result.get("execution_hash") != execution_hash or result.get("boot_id") != boot_id):
            raise ValueError("comfy_resource_owner_mismatch")
        return result

    async def set_pin(self, service_id: UUID, revision: int, enabled: bool):
        if not isinstance(service_id, UUID) or type(revision) is not int or revision < 1 or type(enabled) is not bool:
            raise ValueError("comfy_request_invalid")
        return await self._json("PUT", f"{_RESOURCE}/pins/{service_id}", resource=True,
                                body={"revision": revision, "enabled": enabled})

    async def set_retention(self, service_id: UUID, revision: int, enabled: bool, keep_warm: bool):
        if (not isinstance(service_id, UUID) or type(revision) is not int or revision < 1
                or type(enabled) is not bool or type(keep_warm) is not bool):
            raise ValueError("comfy_request_invalid")
        return await self._json("PUT", f"{_RESOURCE}/retention/{service_id}", resource=True,
                                body={"revision": revision, "enabled": enabled, "keep_warm": keep_warm})

    async def set_automatic(self, service_id: UUID, revision: int, enabled: bool):
        if not isinstance(service_id, UUID) or type(revision) is not int or revision < 1 or type(enabled) is not bool:
            raise ValueError("comfy_request_invalid")
        return await self._json("PUT", f"{_RESOURCE}/automatic/{service_id}", resource=True,
                                body={"revision": revision, "enabled": enabled})

    async def switch(self, operation: str, profile: str | None):
        if (operation not in {"draw", "vision"} or operation == "vision" and profile is not None
                or operation == "draw" and (not isinstance(profile, str) or not 1 <= len(profile) <= 128)):
            raise ValueError("comfy_request_invalid")
        result = await self._json("POST", f"{_RESOURCE}/switch", resource=True,
                                  body={"operation": operation, "profile": profile})
        if result.get("status") not in {"ready", "queued", "deferred"}:
            raise ValueError("comfy_response_invalid")
        return result["status"]

    async def release_idle(self, service_id: UUID) -> str:
        if not isinstance(service_id, UUID):
            raise ValueError("comfy_request_invalid")
        result = await self._json("POST", f"{_RESOURCE}/retention/{service_id}/release", resource=True)
        if result.get("status") not in {"queued", "deferred"}:
            raise ValueError("comfy_response_invalid")
        return result["status"]

    async def release(self, prompt_id: UUID, service_id: UUID, operation: str, execution_hash: str) -> str:
        await self._registration(prompt_id, service_id, operation, execution_hash)
        result = await self._json("POST", f"{_RESOURCE}/jobs/{prompt_id}/release", resource=True)
        status = result.get("status")
        if status not in {"released", "retained", "deferred", "failed"}:
            raise ValueError("comfy_response_invalid")
        return status

    async def submit(self, prompt_id: UUID, service_id: UUID, operation: str, execution_hash: str, graph: dict) -> str:
        if _hash(graph, limit=MAX_GRAPH_BYTES + MAX_IMAGE_DATA) != execution_hash:
            raise ValueError("comfy_execution_hash_mismatch")
        registration = await self._registration(prompt_id, service_id, operation, execution_hash)
        if registration.get("state") != "registered":
            raise ValueError("comfy_already_submitted")
        try:
            begun = await self._json("POST", f"{_RESOURCE}/jobs/{prompt_id}/begin", resource=True,
                body={"service_id": str(service_id), "owner": self.owner, "operation": operation,
                      "execution_hash": execution_hash})
            if begun.get("started") is not True:
                return "unknown"
        except (httpx.HTTPError, ValueError):
            return "unknown"
        try:
            result = await self._json("POST", "/prompt", body={"prompt_id": str(prompt_id),
                "client_id": self.owner, "prompt": graph})
        except httpx.RequestError:
            return "unknown"
        except httpx.HTTPStatusError as error:
            return "rejected" if error.response.status_code == 400 else "unknown"
        if result.get("prompt_id") != str(prompt_id):
            raise ValueError("comfy_job_id_mismatch")
        return "accepted"

    async def query(self, prompt_id: UUID, service_id: UUID, operation: str, execution_hash: str):
        await self._registration(prompt_id, service_id, operation, execution_hash)
        result = await self._json("GET", f"/api/jobs/{prompt_id}", missing=True)
        if result is None:
            return None
        if result.get("id") != str(prompt_id):
            raise ValueError("comfy_job_id_mismatch")
        if result.get("status") in {"completed", "failed", "cancelled"}:
            workflow = result.get("workflow")
            extra = workflow.get("extra_data") if isinstance(workflow, dict) else None
            if not isinstance(extra, dict) or extra.get("client_id") != self.owner:
                raise ValueError("comfy_job_owner_mismatch")
        return result

    async def cancel(self, prompt_id: UUID, service_id: UUID, operation: str, execution_hash: str) -> str:
        await self._registration(prompt_id, service_id, operation, execution_hash)
        result = await self._json("POST", f"{_RESOURCE}/jobs/{prompt_id}/cancel", resource=True)
        if result.get("status") not in {"cancelled", "cancelling", "terminal", "unknown"}:
            raise ValueError("comfy_response_invalid")
        return result["status"]

    async def cancel_queued(self, prompt_id: UUID, service_id: UUID, operation: str, execution_hash: str) -> str:
        await self._registration(prompt_id, service_id, operation, execution_hash)
        result = await self._json("POST", f"{_RESOURCE}/jobs/{prompt_id}/cancel-queued", resource=True)
        if result.get("status") not in {"cancelled", "running", "terminal", "unknown"}:
            raise ValueError("comfy_response_invalid")
        return result["status"]

    async def job_state(self, prompt_id: UUID, service_id: UUID, operation: str, execution_hash: str):
        return await self._registration(prompt_id, service_id, operation, execution_hash)

    async def reject(self, prompt_id: UUID, service_id: UUID, operation: str, execution_hash: str) -> bool:
        await self._registration(prompt_id, service_id, operation, execution_hash)
        result = await self._json("POST", f"{_RESOURCE}/jobs/{prompt_id}/reject", resource=True)
        if result.get("status") not in {"rejected", "deferred"}:
            raise ValueError("comfy_response_invalid")
        return result["status"] == "rejected"

    async def download(self, prompt_id: UUID, service_id: UUID, operation: str, execution_hash: str,
                       node_id: str) -> bytes:
        result = await self.query(prompt_id, service_id, operation, execution_hash)
        filename = owned_output(result, prompt_id, node_id, owner=self.owner)
        body = await self._request("GET", f"{_RESOURCE}/jobs/{prompt_id}/output", resource=True,
                                   params={"filename": filename}, limit=MAX_BYTES)
        return _image(body)

    async def delete_history(self, prompt_id: UUID, service_id: UUID, operation: str, execution_hash: str) -> bool:
        result = await self.query(prompt_id, service_id, operation, execution_hash)
        if result is None:
            return True
        if result.get("status") not in {"completed", "failed", "cancelled"}:
            return False
        await self._json("POST", "/history", body={"delete": [str(prompt_id)]})
        return True

    async def close(self):
        await self.http.aclose()
