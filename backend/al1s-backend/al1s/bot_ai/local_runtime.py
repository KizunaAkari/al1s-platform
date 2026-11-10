import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from al1s.bot_ai.contracts import AiPart, LOCAL_GPU_ERROR_CODES, ProtocolError, TOOL_ARGUMENTS


@dataclass
class _Request:
    envelope: object
    epoch: str
    executor: object
    started: asyncio.Event = field(default_factory=asyncio.Event)
    calls: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    image_guards: dict = field(default_factory=dict)
    fallback_text: str = ''
    pending: set = field(default_factory=set)
    cancelled: bool = False
    task: asyncio.Task | None = None


class _Invocation:
    def __init__(self, runtime, request):
        self.runtime, self.request = runtime, request

    @property
    def epoch(self):
        return self.request.epoch

    def guard_image(self, reference, check, fallback_text=''):
        if not isinstance(reference, str) or not callable(check) or not isinstance(fallback_text, str):
            raise ProtocolError('ai_output_invalid')
        self.request.image_guards[reference] = check
        self.request.fallback_text = fallback_text[:3000]

    async def blocking(self, function, *args, **kwargs):
        if not self.runtime.is_active(self.request.envelope.request_id):
            raise ProtocolError('ai_request_stale')
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self.request.pending.add(task)
        task.add_done_callback(self.request.pending.discard)
        return await asyncio.shield(task)

    async def _effect(self, cache, identity, payload, callback, limit):
        if not self.runtime.is_active(self.request.envelope.request_id):
            return {'status': 'denied', 'code': 'ai_scope_revoked'}
        signature = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                               separators=(',', ':')).encode()).hexdigest()
        previous = cache.get(identity)
        if previous:
            if previous[0] != signature:
                raise ProtocolError('ai_effect_replay_conflict')
            return await asyncio.shield(previous[1])
        if len(cache) >= limit:
            raise ProtocolError('ai_effect_limit')
        async def execute():
            try:
                return await callback()
            except Exception:
                return {'status': 'failed', 'code': 'ai_effect_failed'}
        task = asyncio.create_task(execute())
        cache[identity] = (signature, task)
        self.request.pending.add(task)
        task.add_done_callback(self.request.pending.discard)
        result = await asyncio.shield(task)
        if not self.runtime.is_active(self.request.envelope.request_id):
            return {'status': 'denied', 'code': 'ai_scope_revoked'}
        return result

    async def tool(self, call_id, name, arguments):
        if (not isinstance(call_id, str) or not 1 <= len(call_id) <= 128
                or name not in TOOL_ARGUMENTS or not isinstance(arguments, dict)
                or not arguments.keys() <= TOOL_ARGUMENTS[name]):
            raise ProtocolError('ai_tool_invalid')
        handler = self.runtime._on_tool
        if handler is None:
            return {'status': 'failed', 'code': 'tool_unavailable'}
        return await self._effect(self.request.calls, call_id, [name, arguments],
            lambda: handler(self.request.envelope, call_id, name, arguments), 4)

    async def output(self, sequence, parts):
        if type(sequence) is not int or not 0 <= sequence <= 3 or not isinstance(parts, list) or len(parts) > 8:
            raise ProtocolError('ai_output_invalid')
        parts = [AiPart.model_validate(part).model_dump(exclude_none=True) for part in parts]
        handler = self.runtime._on_output
        if handler is None:
            return {'status': 'failed', 'code': 'output_unavailable'}
        return await self._effect(self.request.outputs, sequence, parts,
            lambda: handler(self.request.envelope, sequence, parts), 4)


class LocalAiRuntime:
    local_runtime = True

    def __init__(self):
        self._services, self._requests = {}, {}
        self._on_tool = self._on_output = self._on_completed = None
        self._on_disconnect = self._on_ready = None

    def set_handlers(self, *, on_tool=None, on_output=None, on_completed=None,
                     on_disconnect=None, on_ready=None):
        if self._services:
            raise ProtocolError('ai_runtime_already_started')
        self._on_tool, self._on_output, self._on_completed = on_tool, on_output, on_completed
        self._on_disconnect, self._on_ready = on_disconnect, on_ready

    async def activate(self, service_id, version, executor):
        if version < 1 or any(req.envelope.service_id == service_id for req in self._requests.values()):
            raise ProtocolError('ai_runtime_busy')
        self._services[service_id] = (version, uuid4().hex, executor)
        if self._on_ready:
            await self._on_ready(service_id, version)

    async def deactivate(self, service_id):
        if any(req.envelope.service_id == service_id for req in self._requests.values()):
            raise ProtocolError('ai_runtime_busy')
        self._services.pop(service_id, None)

    def status(self, service_id):
        service = self._services.get(service_id)
        return {'connected': bool(service), 'bridge_version': service[0] if service else 0}

    def epoch(self, service_id):
        service = self._services.get(service_id)
        return service[1] if service else None

    def output_guard(self, request_id):
        request = self._requests.get(request_id)
        if request is None or not request.image_guards:
            return None
        def allowed(reference):
            check = request.image_guards.get(reference)
            return True if check is None else bool(check())
        return allowed, request.fallback_text

    def is_active(self, request_id):
        request = self._requests.get(request_id)
        if request is None or request.cancelled or datetime.now(UTC) >= request.envelope.deadline:
            return False
        service = self._services.get(request.envelope.service_id)
        return bool(service and service[0] == request.envelope.config_version and service[1] == request.epoch)

    async def dispatch(self, envelope):
        service = self._services.get(envelope.service_id)
        if not service or service[0] != envelope.config_version or datetime.now(UTC) >= envelope.deadline:
            raise ProtocolError('ai_engine_unavailable')
        if envelope.request_id in self._requests:
            raise ProtocolError('ai_request_duplicate')
        request = _Request(envelope, service[1], service[2])
        self._requests[envelope.request_id] = request
        request.task = asyncio.create_task(self._run(request))
        await request.started.wait()

    async def _run(self, request):
        request.started.set()
        status, code = 'failed', None
        try:
            status, code = await request.executor.run(request.envelope, _Invocation(self, request))
            if status not in {'completed', 'failed', 'interrupted', 'unknown'}:
                status, code = 'failed', None
        except asyncio.CancelledError:
            status = 'interrupted'
        except ValueError as error:
            status = 'failed'
            if str(error) in LOCAL_GPU_ERROR_CODES:
                code = str(error)
        except Exception:
            status = 'failed'
        finally:
            if request.pending:
                await asyncio.gather(*tuple(request.pending), return_exceptions=True)
            if request.cancelled:
                status, code = 'interrupted', None
            try:
                if self._on_completed:
                    if code:
                        await self._on_completed(request.envelope, status, code)
                    else:
                        await self._on_completed(request.envelope, status)
            finally:
                self._requests.pop(request.envelope.request_id, None)

    def revoke(self, request_id):
        request = self._requests.get(request_id)
        if request is None:
            raise ProtocolError('ai_request_stale')
        request.cancelled = True

    async def cancel(self, request_id):
        self.revoke(request_id)
        request = self._requests[request_id]
        if request.task and not request.task.done() and not request.task.cancelling():
            request.task.cancel()

    async def clear_session(self, service_id, scope, target):
        service = self._services.get(service_id)
        if not service:
            raise ProtocolError('ai_clear_unavailable')
        if any(req.envelope.service_id == service_id and req.envelope.source.scope_kind == scope
               and req.envelope.source.target_id == target for req in self._requests.values()):
            raise ProtocolError('ai_session_busy')
        return await service[2].clear_session(service_id, scope, target)

    async def sticker_control(self, service_id, command, version):
        service = self._services.get(service_id)
        if not service or service[0] != version:
            raise ProtocolError('ai_sticker_unavailable')
        return await service[2].sticker_control(service_id, command)

    async def close(self):
        requests = tuple(self._requests.values())
        for request in requests:
            request.cancelled = True
            if request.task and not request.task.done() and not request.task.cancelling():
                request.task.cancel()
        if requests:
            await asyncio.gather(*(req.task for req in requests if req.task), return_exceptions=True)
        self._services.clear()
