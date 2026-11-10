import asyncio
import hmac
import json

from .safety import read_output, remove_output

ADAPTER_VERSION = "comfy-0.43-bb878b9-al1s-v8"
MAX_REQUEST = 320 * 1024


async def _body(request):
    if request.content_length is not None and request.content_length > MAX_REQUEST:
        raise ValueError("resource_request_invalid")
    if hasattr(request, "content"):
        raw = await request.content.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError("resource_request_invalid")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("resource_request_invalid") from error
    else:
        value = await request.json()
    if not isinstance(value, dict):
        raise ValueError("resource_request_invalid")
    return value


def _terminal(queue, prompt, owner, *, success_only=False):
    history = queue.get_history(prompt_id=prompt)
    row = history.get(prompt) if isinstance(history, dict) else None
    native = row.get("prompt") if isinstance(row, dict) else None
    status = row.get("status") if isinstance(row, dict) else None
    extra = native[3] if isinstance(native, (tuple, list)) and len(native) > 3 else None
    allowed = {"success"} if success_only else {"success", "error"}
    return bool(isinstance(extra, dict) and extra.get("client_id") == owner
                and isinstance(status, dict) and status.get("status_str") in allowed)


def _owned_output_filename(queue, prompt):
    history = queue.get_history(prompt_id=prompt)
    row = history.get(prompt) if isinstance(history, dict) else None
    outputs = row.get("outputs", {}) if isinstance(row, dict) else {}
    if not outputs:
        return None
    if not isinstance(outputs, dict) or len(outputs) != 1:
        raise ValueError("resource_output_invalid")
    node = next(iter(outputs.values()))
    images = node.get("images") if isinstance(node, dict) else None
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise ValueError("resource_output_invalid")
    image = images[0]
    if (image.get("subfolder") not in (f"al1s-drawing/{prompt}", f"al1s-drawing\\{prompt}")
            or image.get("type") != "output" or not isinstance(image.get("filename"), str)):
        raise ValueError("resource_output_invalid")
    return image["filename"]


def build_handlers(registry, queue, scheduler, *, token, web, output_root, inspect_callback,
                   ready=lambda: True, metrics=lambda: {}, cache=None):
    def endpoint(function):
        async def handle(request):
            authorization = request.headers.get("Authorization", "")
            if not hmac.compare_digest(authorization, "Bearer " + token):
                return web.json_response({"error": "resource_unauthorized"}, status=401)
            if not ready():
                return web.json_response({"error": "resource_unavailable"}, status=503)
            try:
                result = await function(request)
                return web.json_response(result)
            except (ValueError, TypeError, KeyError) as error:
                code = str(error)
                if not code.startswith("resource_") or len(code) > 80:
                    code = "resource_request_invalid"
                return web.json_response({"error": code}, status=409)
        return handle

    @endpoint
    async def state(_request):
        return {**registry.state(), **metrics(), "adapter": ADAPTER_VERSION, "release_status": scheduler.last_status}

    @endpoint
    async def inspect(request):
        data = await _body(request)
        if set(data) != {"nodes", "assets"}:
            raise ValueError("resource_request_invalid")
        result = await asyncio.to_thread(inspect_callback, data["nodes"], data["assets"])
        return {**result, "adapter": ADAPTER_VERSION, "boot_id": registry.boot_id}

    @endpoint
    async def retention(request):
        data = await _body(request)
        if set(data) != {"revision", "enabled", "keep_warm"}:
            raise ValueError("resource_request_invalid")
        registry.set_retention(request.match_info["service"], **data)
        scheduler.refresh()
        return registry.state()

    @endpoint
    async def automatic(request):
        data = await _body(request)
        if set(data) != {"revision", "enabled"}:
            raise ValueError("resource_request_invalid")
        registry.set_automatic(request.match_info["service"], **data)
        scheduler.refresh()
        return registry.state()

    @endpoint
    async def switch(request):
        data = await _body(request)
        if set(data) != {"operation", "profile"}:
            raise ValueError("resource_request_invalid")
        with queue.mutex:
            if queue.queue or queue.currently_running:
                return {"status": "deferred"}
            token = registry.request_switch(data["operation"], data["profile"])
            if token == "deferred":
                return {"status": "deferred"}
            if token is None:
                return {"status": "ready"}
            queue.set_flag("al1s_switch", token)
            scheduler.last_status = "switching"
        return {"status": "queued"}

    @endpoint
    async def release_idle(request):
        with queue.mutex:
            if queue.queue or queue.currently_running:
                return {"status": "deferred"}
            token = registry.request_manual_release(request.match_info["service"])
            if token is None:
                return {"status": "deferred"}
            queue.set_flag("al1s_idle_release", token)
            scheduler.last_status = "queued"
        return {"status": "queued"}

    @endpoint
    async def pin(request):
        data = await _body(request)
        if set(data) != {"revision", "enabled"}:
            raise ValueError("resource_request_invalid")
        registry.set_pin(request.match_info["service"], **data)
        scheduler.refresh()
        return registry.state()

    @endpoint
    async def register(request):
        data = await _body(request)
        if set(data) != {"service_id", "owner", "operation", "execution_hash", "boot_id", "assets"}:
            raise ValueError("resource_request_invalid")
        assets = data["assets"]
        if not isinstance(assets, list) or any(not isinstance(item, dict) for item in assets):
            raise ValueError("resource_request_invalid")
        requested = [{"kind": item.get("kind"), "name": item.get("name")} for item in assets]
        inspected = await asyncio.to_thread(inspect_callback, [], requested)
        if inspected.get("assets") != assets:
            raise ValueError("resource_asset_changed")
        result = registry.register(request.match_info["prompt"], data["service_id"], data["owner"],
            data["operation"], data["execution_hash"], data["boot_id"], assets)
        scheduler.refresh()
        return result

    @endpoint
    async def job(request):
        result = registry.get(request.match_info["prompt"])
        if result is None:
            raise ValueError("resource_job_missing")
        return result

    @endpoint
    async def begin(request):
        data = await _body(request)
        if set(data) != {"service_id", "owner", "operation", "execution_hash"}:
            raise ValueError("resource_request_invalid")
        return {"started": registry.begin(request.match_info["prompt"], data["service_id"], data["owner"],
                                           data["operation"], data["execution_hash"])}

    @endpoint
    async def cancel(request):
        prompt = request.match_info["prompt"]
        row = registry.get(prompt)
        if row is None:
            raise ValueError("resource_job_missing")
        with queue.mutex:
            def owned(item):
                return (len(item) > 3 and item[1] == prompt and isinstance(item[3], dict)
                        and item[3].get("client_id") == row["owner"])
            if any(owned(item) for item in queue.queue) and queue.delete_queue_item(owned):
                registry.mark_dequeued(prompt)
                status = "cancelled"
            elif any(owned(item) for item in queue.currently_running.values()):
                status = "cancelling" if queue.interrupt_if_running(prompt) else "unknown"
            else:
                status = "terminal" if _terminal(queue, prompt, row["owner"]) else "unknown"
        scheduler.refresh()
        return {"status": status}

    @endpoint
    async def cancel_queued(request):
        prompt = request.match_info["prompt"]
        row = registry.get(prompt)
        if row is None:
            raise ValueError("resource_job_missing")
        with queue.mutex:
            def owned(item):
                return (len(item) > 3 and item[1] == prompt and isinstance(item[3], dict)
                        and item[3].get("client_id") == row["owner"])
            if any(owned(item) for item in queue.queue) and queue.delete_queue_item(owned):
                registry.mark_dequeued(prompt)
                status = "cancelled"
            elif any(owned(item) for item in queue.currently_running.values()):
                status = "running"
            else:
                status = "terminal" if _terminal(queue, prompt, row["owner"]) else "unknown"
        scheduler.refresh()
        return {"status": status}

    @endpoint
    async def reject(request):
        prompt = request.match_info["prompt"]
        row = registry.get(prompt)
        if row is None:
            raise ValueError("resource_job_missing")
        with queue.mutex:
            present = (any(item[1] == prompt for item in queue.queue)
                       or any(item[1] == prompt for item in queue.currently_running.values())
                       or bool(queue.get_history(prompt_id=prompt)))
            if present:
                return {"status": "deferred"}
            registry.mark_dequeued(prompt)
        scheduler.refresh()
        return {"status": "rejected"}

    @endpoint
    async def release(request):
        prompt = request.match_info["prompt"]
        row = registry.get(prompt)
        if row is None:
            raise ValueError("resource_job_missing")
        with queue.mutex:
            terminal = _terminal(queue, prompt, row["owner"])
            if terminal and row["operation"] == "draw" and row["state"] not in {"started", "released"}:
                return {"status": "deferred"}
            filename = _owned_output_filename(queue, prompt) if terminal and row["operation"] == "draw" else None
        if filename is not None:
            await asyncio.to_thread(remove_output, output_root, prompt, filename)
        with queue.mutex:
            terminal = _terminal(queue, prompt, row["owner"])
            status = registry.release(prompt, native_terminal=terminal)
        scheduler.refresh()
        if status == "retained" and cache is not None and cache.pending(prompt):
            status = "deferred"
        return {"status": status}

    async def output(request):
        authorization = request.headers.get("Authorization", "")
        if not hmac.compare_digest(authorization, "Bearer " + token):
            return web.json_response({"error": "resource_unauthorized"}, status=401)
        if not ready():
            return web.json_response({"error": "resource_unavailable"}, status=503)
        try:
            prompt = request.match_info["prompt"]
            row = registry.get(prompt)
            if row is None or not _terminal(queue, prompt, row["owner"], success_only=True):
                raise ValueError("resource_output_invalid")
            body = await asyncio.to_thread(read_output, output_root, prompt, request.query.get("filename", ""),
                                           max_bytes=8 * 1024 * 1024)
            return web.Response(body=body, content_type="image/png")
        except (ValueError, OSError):
            return web.json_response({"error": "resource_output_invalid"}, status=409)

    return {"state": state, "inspect": inspect, "retention": retention, "automatic": automatic, "switch": switch, "release_idle": release_idle, "pin": pin, "register": register,
            "job": job, "begin": begin, "cancel": cancel, "cancel_queued": cancel_queued,
            "reject": reject, "release": release, "output": output}


def install_routes(routes, handlers):
    prefix = "/al1s/resources/v1"
    for method, path, name in (("get", "/state", "state"), ("post", "/inspect", "inspect"),
            ("put", "/retention/{service}", "retention"), ("put", "/automatic/{service}", "automatic"),
            ("post", "/switch", "switch"),
            ("post", "/retention/{service}/release", "release_idle"),
            ("put", "/pins/{service}", "pin"),
            ("put", "/jobs/{prompt}", "register"), ("get", "/jobs/{prompt}", "job"),
            ("post", "/jobs/{prompt}/begin", "begin"), ("post", "/jobs/{prompt}/cancel", "cancel"),
            ("post", "/jobs/{prompt}/cancel-queued", "cancel_queued"),
            ("post", "/jobs/{prompt}/reject", "reject"),
            ("post", "/jobs/{prompt}/release", "release"), ("get", "/jobs/{prompt}/output", "output")):
        getattr(routes, method)(prefix + path)(handlers[name])
