import asyncio
import hashlib
import json
import logging
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from al1s.bot_drawing.moderation import AuditDecision
from al1s.bot_drawing.runner import DrawingTaskRunner, PreparedExecution


@pytest.mark.parametrize(('status', 'error', 'expected'), [
    (400, {'code': 'moderation_blocked'}, 'drawing_online_moderation_blocked'),
    (403, {'code': 'content_policy_violation'}, 'drawing_online_moderation_blocked'),
    (401, {'code': 'invalid_api_key'}, 'drawing_online_auth_failed'),
    (429, {'code': 'insufficient_quota'}, 'drawing_online_quota_exceeded'),
    (429, {'code': 'organization_usage_limit_exceeded'}, 'drawing_online_quota_exceeded'),
    (429, {'code': 'credit_balance_exhausted', 'type': 'rate_limit_error'}, 'drawing_online_quota_exceeded'),
    (429, {'code': 'project_spend_limit_exceeded'}, 'drawing_online_quota_exceeded'),
    (429, {'code': 'organization_spend_limit_exceeded'}, 'drawing_online_quota_exceeded'),
    (429, {'type': 'insufficient_quota'}, 'drawing_online_quota_exceeded'),
    (429, {'code': 'rate_limit_exceeded'}, 'drawing_online_rate_limited'),
    (429, {'code': 'slow_down', 'type': 'insufficient_quota'}, 'drawing_online_rate_limited'),
    (429, {'type': 'rate_limit_exceeded'}, 'drawing_online_rate_limited'),
    (404, {'code': 'model_not_found'}, 'drawing_online_model_unavailable'),
    (403, {'type': 'permission_error'}, 'drawing_online_access_denied'),
    (400, {'message': 'Your organization must be verified to use this model.'}, 'drawing_online_org_verification'),
    (400, b'not json', 'drawing_online_http_400'),
    (400, b'__broken_stream__', 'drawing_online_http_400'),
    (400, {'code': 'moderation_blocked', 'message': 'x' * 17000}, 'drawing_online_http_400'),
    (400, {'code': 'sk-private-test-key'}, 'drawing_online_http_400'),
    (400, {'message': 'prompt: your organization must be verified'}, 'drawing_online_http_400'),
    (302, {}, 'drawing_online_http_302'),
    (429, {}, 'drawing_online_http_429'),
    (429, {'code': 'unknown_code', 'type': 'insufficient_quota'}, 'drawing_online_http_429'),
    (503, {}, 'drawing_online_http_503'),
])
def test_online_http_errors_are_safely_classified_without_retry(monkeypatch, caplog, status, error, expected):
    caplog.set_level(logging.INFO)
    identity, service, preset = uuid4(), uuid4(), uuid4()
    row = {'id': identity, 'service_id': service, 'preset_id': preset, 'preset_revision': 1,
           'origin': 'admin_preview', 'operation': 'draw', 'generation_status': 'submitting',
           'prompt_status': 'ready', 'positive': 'private-drawing-prompt', 'negative': '',
           'audit_mode': 'r18', 'input_reference': None}
    calls, requests = [], []
    class Repo:
        def task_for_worker(self, _): return row.copy()
        def online_task_allowed(self, _): return True
        def online_credentials(self, *_):
            return ({'model_id': 'gpt-image-2.6-flare', 'base_url': '', 'network_mode': 'direct',
                     'size': '1024x1024', 'quality': 'high'}, 'sk-private-test-key')
        def online_state(self, _, state): calls.append(('state', state)); return True
        def fail_online(self, _, code, state, details=None): calls.append(('failed', code, state, details)); return True
    async def provider(request):
        requests.append(request)
        if error == b'__broken_stream__':
            class Broken(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'{"error":'
                    raise httpx.ReadError('private-drawing-prompt sk-private-test-key', request=request)
            return httpx.Response(status, stream=Broken())
        payload = error if isinstance(error, bytes) else json.dumps({'error': {**error,
            'message': error.get('message', 'private-drawing-prompt sk-private-test-key')}}).encode()
        return httpx.Response(status, content=payload)
    original = httpx.AsyncClient
    def client(**kwargs):
        kwargs.pop('proxy', None)
        return original(transport=httpx.MockTransport(provider), **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    runner = DrawingTaskRunner(Repo(), None, None, None, None, lambda _: None)
    asyncio.run(runner._execute_online(identity))
    assert calls[-1] == ('failed', expected, 'failed', None)
    assert len(requests) == 1 and ('state', 'sent') in calls
    assert 'private-drawing-prompt' not in caplog.text and 'sk-private-test-key' not in caplog.text


@pytest.mark.parametrize(('error_code', 'details', 'expected'), [
    ('drawing_online_moderation_blocked', {'stage': 'input', 'categories': ['sexual']}, '输入审核'),
    ('drawing_online_moderation_blocked', {'stage': 'output', 'categories': ['violence']}, '结果未通过'),
    ('drawing_online_moderation_blocked', None, '内容审核'),
    ('drawing_online_auth_failed', None, '认证失败'),
    ('drawing_online_quota_exceeded', None, '额度或预算不足'),
    ('drawing_online_rate_limited', None, '请求过于频繁'),
    ('drawing_online_model_unavailable', None, '模型不可用'),
    ('drawing_online_access_denied', None, '权限'),
    ('drawing_online_org_verification', None, '组织验证'),
    ('drawing_online_http_400', None, 'HTTP 400'),
    ('drawing_online_http_429', None, 'HTTP 429'),
    ('drawing_online_http_503', None, 'HTTP 503'),
])
def test_online_qq_failure_notice_distinguishes_provider_errors(error_code, details, expected):
    row = {'id': uuid4(), 'service_id': uuid4(), 'origin': 'qq', 'scope_kind': 'qq_group',
           'target_id': '123', 'sender_id': '200', 'source_message_id': '456', 'bot_self_id': '100',
           'drawing_version': 1, 'reply_kind': 'failure_notice', 'error_code': error_code,
           'online_moderation_details': details}
    calls = []
    class Repo:
        def task_for_worker(self, _): return row.copy()
    class Sender:
        async def send(self, _envelope, _sequence, parts, _client, **_kwargs):
            calls.append(parts)
            return {'status': 'sent'}
    runner = DrawingTaskRunner(Repo(), None, None, None, Sender(), lambda _: object())
    asyncio.run(runner.deliver(row['id'], 'result'))
    assert len(calls) == 1 and expected in calls[0][0]['text']
    assert 'sexual' not in calls[0][0]['text'] and 'violence' not in calls[0][0]['text']


@pytest.mark.parametrize(('details', 'expected'), [
    ({'moderation_stage': 'input', 'categories': ['sexual', 'violence']},
     {'stage': 'input', 'categories': ['sexual', 'violence']}),
    ({'moderation_stage': 'output', 'categories': []}, {'stage': 'output', 'categories': []}),
    ({'moderation_stage': 'unknown', 'categories': ['private-secret-value']},
     {'stage': 'unknown', 'categories': ['unrecognized']}),
    ({'moderation_stage': 'invalid-private-value', 'categories': None}, None),
    (None, None),
])
def test_online_moderation_details_are_bounded_and_safe(details, expected):
    from al1s.bot_drawing.runner import _online_error_response
    response = httpx.Response(400, json={'error': {'code': 'moderation_blocked',
        'message': 'private-prompt-and-secret', 'moderation_details': details}})
    code, parsed = asyncio.run(_online_error_response(response))
    assert code == 'drawing_online_moderation_blocked' and parsed == expected
    assert 'private-' not in str(parsed)


def test_queue_notice_lists_both_queues_without_task_details():
    from al1s.bot_ai.gpu_queue import GpuQueue
    async def run():
        current, running, other = uuid4(), uuid4(), uuid4()
        row = {'id': current, 'service_id': uuid4(), 'origin': 'qq', 'scope_kind': 'qq_group',
               'target_id': '123', 'sender_id': '200', 'source_message_id': '456', 'bot_self_id': '100',
               'drawing_version': 1, 'operation': 'draw', 'endpoint_key': 'openai-images'}
        class Repo:
            def task_for_worker(self, _): return row.copy()
            def online_positions(self): return {str(running): 0, str(current): 1, str(other): 2}
        class Sender:
            def __init__(self): self.parts = None
            async def send(self, _envelope, _sequence, parts, _client, **_kwargs):
                self.parts = parts
                return {'status': 'sent'}
        queue, sender = GpuQueue(), Sender()
        queue.reserve('vision:private-job')
        queue.reserve('private-drawing-job')
        runner = DrawingTaskRunner(Repo(), None, None, None, sender, lambda _: object(), gpu_queue=queue)
        runner._online_needs_recovery = False
        runner._online_task_id = running
        await runner.deliver(current, 'notice')
        text = sender.parts[0]['text']
        assert text == ('当前任务队列\n本地（共享 GPU）\n1. 处理中\n2. 等待中\n'
                        '线上绘图\n1. 处理中\n2. 等待中（本次）\n3. 等待中')
        assert all(value not in text for value in ('private-job', 'private-drawing-job', str(running), str(other)))
    asyncio.run(run())


def test_queue_notice_marks_local_task_and_handles_unknown_online_status():
    from al1s.bot_ai.gpu_queue import GpuQueue
    async def run():
        current = uuid4()
        row = {'id': current, 'service_id': uuid4(), 'origin': 'qq', 'scope_kind': 'qq_group',
               'target_id': '123', 'sender_id': '200', 'source_message_id': '456', 'bot_self_id': '100',
               'drawing_version': 1, 'operation': 'draw', 'endpoint_key': 'comfy-local-8188'}
        class Repo:
            def task_for_worker(self, _): return row.copy()
            def online_positions(self): raise ValueError('temporary database failure')
        class Sender:
            def __init__(self): self.text = None
            async def send(self, _envelope, _sequence, parts, _client, **_kwargs):
                self.text = parts[0]['text']
                return {'status': 'sent'}
        queue, sender = GpuQueue(), Sender()
        queue.reserve(str(current))
        runner = DrawingTaskRunner(Repo(), None, None, None, sender, lambda _: object(), gpu_queue=queue)
        await runner.deliver(current, 'notice')
        assert '本地（共享 GPU）\n1. 处理中（本次）' in sender.text
        assert '线上绘图\n状态暂不可用' in sender.text
    asyncio.run(run())


def test_queue_notice_during_recovery_does_not_invent_local_order():
    from al1s.bot_ai.gpu_queue import GpuQueue
    async def run():
        current = uuid4()
        row = {'id': current, 'service_id': uuid4(), 'origin': 'qq', 'scope_kind': 'qq_group',
               'target_id': '123', 'sender_id': '200', 'source_message_id': '456', 'bot_self_id': '100',
               'drawing_version': 1, 'operation': 'draw', 'endpoint_key': 'openai-images'}
        class Repo:
            def task_for_worker(self, _): return row.copy()
            def online_positions(self): return {str(current): 0}
        class Sender:
            def __init__(self): self.text = None
            async def send(self, _envelope, _sequence, parts, _client, **_kwargs):
                self.text = parts[0]['text']
                return {'status': 'sent'}
        sender = Sender()
        runner = DrawingTaskRunner(Repo(), None, None, None, sender, lambda _: object(),
                                   gpu_queue=GpuQueue(recovering=True))
        await runner.deliver(current, 'notice')
        assert '本地（共享 GPU）\n状态暂不可用' in sender.text
        assert '线上绘图\n1. 状态待核对（本次）' in sender.text
    asyncio.run(run())


@pytest.mark.parametrize('active_queue', ['local', 'online'])
def test_queue_notice_omits_idle_queue(active_queue):
    from al1s.bot_ai.gpu_queue import GpuQueue
    async def run():
        current = uuid4()
        row = {'id': current, 'service_id': uuid4(), 'origin': 'qq', 'scope_kind': 'qq_group',
               'target_id': '123', 'sender_id': '200', 'source_message_id': '456', 'bot_self_id': '100',
               'drawing_version': 1, 'operation': 'draw', 'endpoint_key': 'openai-images'}
        class Repo:
            def task_for_worker(self, _): return row.copy()
            def online_positions(self): return {str(current): 0} if active_queue == 'online' else {}
        class Sender:
            def __init__(self): self.text = None
            async def send(self, _envelope, _sequence, parts, _client, **_kwargs):
                self.text = parts[0]['text']
                return {'status': 'sent'}
        queue, sender = GpuQueue(), Sender()
        if active_queue == 'local':
            queue.reserve(str(current))
        runner = DrawingTaskRunner(Repo(), None, None, None, sender, lambda _: object(), gpu_queue=queue)
        runner._online_needs_recovery = False
        if active_queue == 'online':
            runner._online_task_id = current
        await runner.deliver(current, 'notice')
        expected = '本地（共享 GPU）' if active_queue == 'local' else '线上绘图'
        omitted = '线上绘图' if active_queue == 'local' else '本地（共享 GPU）'
        assert expected in sender.text and '处理中（本次）' in sender.text
        assert omitted not in sender.text and '空闲' not in sender.text
    asyncio.run(run())


def test_queue_notice_is_not_sent_if_both_queues_are_idle():
    from al1s.bot_ai.gpu_queue import GpuQueue
    async def run():
        current = uuid4()
        row = {'id': current, 'service_id': uuid4(), 'origin': 'qq', 'scope_kind': 'qq_group',
               'target_id': '123', 'sender_id': '200', 'source_message_id': '456', 'bot_self_id': '100',
               'drawing_version': 1, 'operation': 'draw', 'endpoint_key': 'openai-images'}
        class Repo:
            def task_for_worker(self, _): return row.copy()
            def online_positions(self): return {}
        class Sender:
            def __init__(self): self.calls = 0
            async def send(self, *_args, **_kwargs): self.calls += 1
        sender = Sender()
        runner = DrawingTaskRunner(Repo(), None, None, None, sender, lambda _: object(), gpu_queue=GpuQueue())
        await runner.deliver(current, 'notice')
        assert sender.calls == 0
    asyncio.run(run())


def test_unfinished_previous_audit_returns_unknown_within_timeout():
    async def run():
        pending = asyncio.get_running_loop().create_future()
        class Auditor:
            timeout = 0.01
            _pending = pending
            async def audit(self, _body):
                raise AssertionError("the prior audit has not stopped")
        runner = DrawingTaskRunner(None, None, None, Auditor(), None, lambda _: None)
        decision = await asyncio.wait_for(runner._audit(b"test-image"), timeout=0.5)
        assert decision.status == "unknown" and decision.image_sha256 == hashlib.sha256(b"test-image").hexdigest()
        pending.cancel()
    asyncio.run(run())


def task():
    return {"id": uuid4(), "service_id": uuid4(), "prompt_id": uuid4(), "operation": "tag",
        "origin": "qq", "generation_status": "queued", "resource_registration_state": "pending",
        "row_version": 1, "boot_id": None, "execution_hash": None, "prompt_transport_started": False,
        "scope_kind": "qq_group", "target_id": "123", "sender_id": "200", "source_message_id": "456",
        "ai_version": 1, "drawing_version": 1, "audit_mode": "green", "processing_stage": "preparing_resources"}


def test_comfy_progress_only_uses_events_for_the_owned_prompt(monkeypatch):
    import json
    from al1s.bot_drawing import runner as drawing_runner

    async def run():
        row, allow, received, ready = task(), asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Socket:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return None
            def __aiter__(self): return self.events()
            async def events(self):
                yield json.dumps({"type": "executing", "data": {"prompt_id": str(uuid4()), "node": "1"}})
                await allow.wait()
                yield json.dumps({"type": "executing", "data": {"prompt_id": str(row["prompt_id"]), "node": "1"}})
                yield json.dumps({"type": "progress", "data": {"prompt_id": str(row["prompt_id"]),
                                                                  "node": "1", "value": 3, "max": 10}})
                received.set()
                await asyncio.Event().wait()
        monkeypatch.setattr(drawing_runner, "connect", lambda *_args, **_kwargs: Socket())
        runner = DrawingTaskRunner(None, None, None, None, None, lambda _: None)
        watcher = asyncio.create_task(runner._watch_progress(row, "http://127.0.0.1:8188", "owner",
                                                             {"1": {"class_type": "KSampler"}}, ready))
        await asyncio.wait_for(ready.wait(), timeout=1)
        await asyncio.sleep(0)
        assert runner.progress(row["id"]) is None
        allow.set()
        await asyncio.wait_for(received.wait(), timeout=1)
        progress = runner.progress(row["id"])
        assert (progress["node_id"], progress["node_name"], progress["value"], progress["max"]) == ("1", "KSampler", 3, 10)
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        assert runner.progress(row["id"]) is None
    asyncio.run(run())


def test_drawing_switches_models_before_submitting_and_keeps_its_gpu_turn_while_waiting():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        row, calls, queue = task(), [], GpuQueue()
        row.update(operation="draw", preset_id=uuid4(), preset_revision=1,
                   generation_status="submitting", resource_registration_state="confirmed",
                   boot_id="boot-1", execution_hash="a" * 64, row_version=4)
        queue.reserve(str(row["id"]), owner=(row["service_id"], row["sender_id"]))
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def current_mode(self, *_): return "green"
            def defer_check(self, *_args, **_kwargs): calls.append("deferred")
            def begin_submit(self, *_args, **_kwargs): calls.append("begin"); return 5
            def transition(self, *_args, **_kwargs): calls.append("transition"); return 6
        class Resource:
            ready = False
            async def state(self): return {"boot_id": "boot-1", "adapter": "v8"}
            async def switch(self, operation, profile):
                calls.append(("switch", operation, profile))
                return "ready" if self.ready else "queued"
            async def submit(self, *_): calls.append("submit"); return "accepted"
            async def close(self): pass
        resource = Resource()
        class Config:
            async def client(self, _): return resource
        runner = DrawingTaskRunner(Repo(), Config(), None, None, None, lambda _: None, gpu_queue=queue)
        async def build(_row): return PreparedExecution({"1": {}}, "a" * 64, [], [])
        runner._build_execution = build
        assert await runner.advance(row["id"]) == "waiting_for_resources"
        assert "begin" not in calls and queue.active == str(row["id"])
        resource.ready = True
        assert await runner.advance(row["id"]) == "running"
        assert calls.index("begin") < calls.index("submit")

    asyncio.run(run())


def test_tag_waits_when_native_resource_coordinator_reports_an_active_task():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        row, calls, queue = task(), [], GpuQueue()
        row.update(generation_status='submitting', resource_registration_state='confirmed',
                   boot_id='boot-1', execution_hash='a' * 64, row_version=4)
        queue.reserve(str(row['id']))

        class Repo:
            def task_for_worker(self, _): return dict(row)
            def current_mode(self, *_): return 'green'
            def defer_check(self, *_args, **_kwargs): calls.append('deferred')
            def begin_submit(self, *_args, **_kwargs): calls.append('begin'); return 5

        class Resource:
            async def state(self): return {'boot_id': 'boot-1', 'adapter': 'v8'}
            async def switch(self, operation, profile): calls.append((operation, profile)); return 'deferred'
            async def close(self): pass

        class Config:
            async def client(self, _): return Resource()

        runner = DrawingTaskRunner(Repo(), Config(), None, None, None, lambda _: None, gpu_queue=queue)
        assert await runner.advance(row['id']) == 'waiting_for_resources'
        assert ('draw', 'tag') in calls and 'begin' not in calls
        assert queue.active == str(row['id'])

    asyncio.run(run())


def test_queued_notice_is_sent_before_slow_resource_registration():
    async def run():
        noticed, block_registration = asyncio.Event(), asyncio.Event()
        task_id = uuid4()

        class Repo:
            def expire_next(self, _): return None
            def next_cleanup(self, _): return None
            def next_registration(self, _, expected=None): return task_id
            def next_delivery(self, _): return (task_id, 'notice')

        runner = DrawingTaskRunner(Repo(), None, None, None, None, lambda _: None)

        async def register(_task):
            await block_registration.wait()

        async def deliver(_task, kind):
            assert kind == 'notice'
            noticed.set()

        runner.register, runner.deliver = register, deliver
        worker = asyncio.create_task(runner._run())
        try:
            await asyncio.wait_for(noticed.wait(), timeout=1)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


def test_slow_registration_does_not_block_delivery_or_queue_loop():
    async def run():
        started, deliver_second, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        identity, count = uuid4(), 0
        class Repo:
            def expire_next(self, _): return None
            def next_cleanup(self, _): return None
            def next_registration(self, _, expected=None): return identity
            def next_active(self, _): return None
            def claim_next(self, *_): return None
            def next_deletion(self): return None
            def next_media_cleanup(self): return None
            def purge_records(self, **_kwargs): return 0
            def next_delivery(self, _):
                nonlocal count
                count += 1
                return (identity, 'result') if started.is_set() else None
        runner = DrawingTaskRunner(Repo(), None, None, None, None, lambda _: None)
        async def register(_task):
            started.set()
            await release.wait()
        async def deliver(_task, _kind): deliver_second.set()
        runner.register, runner.deliver = register, deliver
        runner.start()
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.wait_for(deliver_second.wait(), timeout=1)
            assert count >= 2 and not release.is_set()
        finally:
            await runner.close()
    asyncio.run(run())


def test_registration_selects_the_gpu_owner_when_database_order_differs():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        first, second, started = uuid4(), uuid4(), asyncio.Event()
        queue = GpuQueue()
        queue.reserve(str(first)); queue.reserve(str(second))
        class Repo:
            def expire_next(self, _): return None
            def next_cleanup(self, _): return None
            def next_registration(self, _endpoint, expected):
                assert expected == first
                return expected
            def next_active(self, _): return None
            def claim_next(self, *_): return None
            def task_for_worker(self, _): return {'generation_status': 'queued', 'cleanup_status': 'none'}
            def next_delivery(self, _): return None
            def next_deletion(self): return None
            def next_media_cleanup(self): return None
            def purge_records(self, **_): return 0
        runner = DrawingTaskRunner(Repo(), None, None, None, None, lambda _: None, gpu_queue=queue)
        async def register(identity):
            assert identity == first
            started.set()
            await asyncio.Event().wait()
        runner.register = register
        runner.start()
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
        finally:
            await runner.close()
    asyncio.run(run())


def test_gpu_turn_is_released_only_after_own_native_cleanup_is_confirmed():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        row, queue = task(), GpuQueue()
        queue.reserve(str(row["id"]), owner=(row["service_id"], row["sender_id"]))
        queue.reserve("next")
        class Repo:
            def mark_cleanup(self, _id, expected_version, status): return expected_version + 1
        class Resource:
            status = "deferred"
            async def release(self, *_): return self.status
            async def delete_history(self, *_): return True
        runner = DrawingTaskRunner(Repo(), None, None, None, None, None, gpu_queue=queue)
        resource = Resource()
        _, cleaned = await runner._release(row | {"execution_hash": "a" * 64}, resource, 1)
        assert not cleaned and queue.active == str(row["id"])
        resource.status = "retained"
        _, cleaned = await runner._release(row | {"execution_hash": "a" * 64}, resource, 1)
        assert cleaned and queue.active == "next"

    asyncio.run(run())


def test_cpu_audit_does_not_hold_gpu_after_output_is_saved_and_native_cleanup_confirms():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        row, queue, audit_started, release_audit = task(), GpuQueue(), asyncio.Event(), asyncio.Event()
        row.update(operation="draw", origin="admin_preview", generation_status="running", audit_mode="green",
                   execution_hash="a" * 64, row_version=1)
        queue.reserve(str(row["id"]))
        queue.reserve("vision:next")
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def reserve_output(self, *_args, expected_version, **_kwargs): return expected_version + 1
            def set_stage(self, _id, _stage, expected_version):
                row["row_version"] = expected_version + 1
                return row["row_version"]
            def transition(self, _id, _before, after, expected_version, **kwargs):
                row.update(generation_status=after, row_version=expected_version + 1, **kwargs)
                return row["row_version"]
            def mark_cleanup(self, _id, expected_version, status): return expected_version + 1
            def record_audit(self, _id, expected_version, decision): return expected_version + 1
            def set_result_ready(self, _id, expected_version, reply_kind): return expected_version + 1
            def mark_preview_validated(self, *_args, **_kwargs): pass
        class Media:
            def put(self, _id, _kind, body): return "output.png", hashlib.sha256(body).hexdigest()
        class Auditor:
            async def audit(self, body):
                audit_started.set()
                await release_audit.wait()
                return AuditDecision("allow", hashlib.sha256(body).hexdigest())
        class Resource:
            calls = 0
            async def download(self, *_): return b"test-image"
            async def release(self, *_):
                self.calls += 1
                return "deferred" if self.calls == 1 else "retained"
            async def delete_history(self, *_): return True
            async def state(self): return {"peak_allocated_bytes": 1}
        runner, resource = DrawingTaskRunner(Repo(), None, Media(), Auditor(), None, None, gpu_queue=queue), Resource()
        job = asyncio.create_task(runner.complete(row["id"], {"status": "completed"}, resource, output_node="1"))
        await asyncio.wait_for(audit_started.wait(), 1)
        assert queue.active == "vision:next" and resource.calls == 2
        release_audit.set()
        await job
    asyncio.run(run())


def test_audit_failure_after_gpu_release_leaves_result_pending_for_recovery():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        row, queue, cleanup = task(), GpuQueue(), []
        row.update(operation="draw", origin="admin_preview", generation_status="running", audit_mode="green",
                   execution_hash="a" * 64, row_version=1)
        queue.reserve(str(row["id"]))
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def reserve_output(self, *_args, expected_version, **_kwargs): return expected_version + 1
            def set_stage(self, _id, _stage, expected_version):
                row["row_version"] = expected_version + 1
                return row["row_version"]
            def transition(self, _id, _before, after, expected_version, **kwargs):
                row.update(generation_status=after, row_version=expected_version + 1, **kwargs)
                return row["row_version"]
            def mark_cleanup(self, _id, expected_version, status):
                cleanup.append(status)
                row["row_version"] = expected_version + 1
                return row["row_version"]
        class Media:
            def put(self, _id, _kind, body): return "output.png", hashlib.sha256(body).hexdigest()
        class Auditor:
            async def audit(self, _body): raise ValueError("temporary audit failure")
        class Resource:
            async def download(self, *_): return b"test-image"
            async def release(self, *_): return "retained"
            async def delete_history(self, *_): return True
        runner = DrawingTaskRunner(Repo(), None, Media(), Auditor(), None, None, gpu_queue=queue)
        await runner.complete(row["id"], {"status": "completed"}, Resource(), output_node="1")
        assert cleanup == ["pending"] and queue.idle and row["generation_status"] == "succeeded"
    asyncio.run(run())


def test_cancelled_unsubmitted_drawing_leaves_the_shared_queue_immediately():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        row, queue = task(), GpuQueue()
        queue.reserve("current")
        queue.reserve(str(row["id"]))
        class Repo:
            def task_for_worker(self, _): return {"generation_status": "cancelled", "cleanup_status": "done"}
        runner = DrawingTaskRunner(Repo(), None, None, None, None, None, gpu_queue=queue)
        await runner.cancelled(row["id"])
        assert queue.active == "current" and queue.pending == 0
    asyncio.run(run())


def test_runner_reconciles_a_terminal_ticket_without_waiting_for_another_request():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        row, queue = task(), GpuQueue()
        queue.reserve(str(row["id"]))
        class Repo:
            def expire_next(self, _): return None
            def next_cleanup(self, _): return None
            def next_registration(self, _, expected=None): return None
            def next_active(self, _): return None
            def claim_next(self, *_): return None
            def task_for_worker(self, _): return {"generation_status": "cancelled", "cleanup_status": "done"}
            def next_delivery(self, _): return None
            def next_media_cleanup(self): return None
            def purge_records(self, **_): pass
        runner = DrawingTaskRunner(Repo(), None, None, None, None, None, gpu_queue=queue)
        runner.start()
        async with asyncio.timeout(1):
            while not queue.idle:
                await asyncio.sleep(0.01)
        await runner.close()
    asyncio.run(run())


def test_unsubmitted_cancelled_task_can_release_its_slot_after_comfy_restarts():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        row, queue, changes = task(), GpuQueue(), []
        row.update(generation_status="cancelled", resource_registration_state="confirmed", boot_id="old",
                   execution_hash="a" * 64, prompt_transport_started=False, reply_kind="failure_notice")
        queue.reserve(str(row["id"]))
        queue.reserve("next")
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def mark_cleanup(self, _id, expected_version, status): changes.append(status); return expected_version + 1
        class Resource:
            async def state(self): return {"boot_id": "new"}
            async def close(self): pass
        class Config:
            async def client(self, _): return Resource()
        runner = DrawingTaskRunner(Repo(), Config(), None, None, None, None, gpu_queue=queue)
        await runner.recover_cleanup(row["id"])
        assert changes == ["done"] and queue.active == "next"
    asyncio.run(run())


def test_native_queue_wait_does_not_consume_the_execution_timeout():
    from datetime import UTC, datetime, timedelta

    async def run():
        row, calls = task(), []
        row.update(generation_status="running", resource_registration_state="confirmed", boot_id="boot-1",
                   execution_hash="a" * 64, prompt_transport_started=True,
                   started_at=datetime.now(UTC) - timedelta(minutes=6),
                   expires_at=datetime.now(UTC) + timedelta(minutes=20))
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def defer_check(self, *_args, **_kwargs): calls.append("deferred")
            def transition(self, *_args, **_kwargs): calls.append("cancelled")
        class Resource:
            async def state(self): return {"boot_id": "boot-1"}
            async def query(self, *_): return {"status": "pending"}
            async def close(self): pass
        class Config:
            async def client(self, _): return Resource()
        runner = DrawingTaskRunner(Repo(), Config(), None, None, None, lambda _: None)
        assert await runner.advance(row["id"]) == "pending"
        assert calls == ["deferred"]

    asyncio.run(run())


def test_execution_timeout_starts_when_native_job_actually_runs():
    async def run():
        row, calls = task(), []
        row.update(generation_status="running", resource_registration_state="confirmed", boot_id="boot-1",
                   execution_hash="a" * 64, prompt_transport_started=True, started_at=None, expires_at=None)
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def mark_native_started(self, _id, expected_version):
                calls.append("started")
                return expected_version + 1
            def defer_check(self, _id, expected_version, seconds=2): calls.append(("deferred", expected_version))
        class Resource:
            async def state(self): return {"boot_id": "boot-1"}
            async def query(self, *_): return {"status": "in_progress"}
            async def close(self): pass
        class Config:
            async def client(self, _): return Resource()
        runner = DrawingTaskRunner(Repo(), Config(), None, None, None, lambda _: None)
        assert await runner.advance(row["id"]) == "pending"
        assert calls == ["started", ("deferred", 2)]

    asyncio.run(run())


def test_model_switch_wait_is_bounded_by_the_queue_deadline():
    from datetime import UTC, datetime, timedelta
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        row, queue, calls = task(), GpuQueue(), []
        row.update(operation="draw", preset_id=uuid4(), preset_revision=1, generation_status="submitting",
                   resource_registration_state="confirmed", boot_id="boot-1", execution_hash="a" * 64,
                   expires_at=datetime.now(UTC) - timedelta(seconds=1))
        queue.reserve(str(row["id"]))
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def current_mode(self, *_): return "green"
            def transition(self, _id, before, after, **kwargs):
                calls.append((before, after, kwargs["error_code"]))
                return 2
            def set_result_ready(self, *_args, **_kwargs): return 3
            def mark_cleanup(self, *_args, **_kwargs): return 4
        class Config:
            async def client(self, _): pytest.fail("expired task must not request model resources")
        runner = DrawingTaskRunner(Repo(), Config(), None, None, None, None, gpu_queue=queue)
        assert await runner.advance(row["id"]) == "expired"
        assert calls == [("submitting", "failed", "drawing_queue_expired")]
    import pytest
    asyncio.run(run())


def test_wish_expansion_receives_positive_text_and_retains_marked_negative():
    async def run():
        row, calls = task(), []
        row.update(wish_text='一只猫【NP】水印', negative='水印', prompt_workflow_name='默认工作流',
                   prompt_aliases=[], requested_lora_keys=[])
        class Repo:
            def begin_prompt_expansion(self, _task_id): return ('profile', 1)
            def task_for_worker(self, _task_id): return dict(row)
            def finish_prompt_expansion(self, _task_id, _profile, _revision, positive, negative):
                calls.append(('result', positive, negative))
        class Prompt:
            async def expand_drawing(self, _service, _profile, _revision, text, intent, **_kwargs):
                calls.append(('input', text, intent.negative))
                return 'orange cat', intent.negative
        runner = DrawingTaskRunner(Repo(), None, None, None, None, None, prompt_model=Prompt())
        await runner._expand_prompt(row['id'])
        assert calls == [('input', '一只猫', '水印'), ('result', 'orange cat', '水印')]
    asyncio.run(run())


def test_wish_expansion_does_not_reinterpret_unparsed_older_tasks():
    async def run():
        row, calls = task(), []
        row.update(wish_text='旧任务【NP】水印', negative='', prompt_workflow_name='默认工作流',
                   prompt_aliases=[], requested_lora_keys=[])
        class Repo:
            def begin_prompt_expansion(self, _task_id): return ('profile', 1)
            def task_for_worker(self, _task_id): return dict(row)
            def finish_prompt_expansion(self, *_args): pass
        class Prompt:
            async def expand_drawing(self, _service, _profile, _revision, text, _intent, **_kwargs):
                calls.append(text)
                return 'cat', ''
        runner = DrawingTaskRunner(Repo(), None, None, None, None, None, prompt_model=Prompt())
        await runner._expand_prompt(row['id'])
        assert calls == ['旧任务【NP】水印']
    asyncio.run(run())


def test_uncertain_registration_is_persisted_without_acknowledging_or_submitting():
    async def run():
        row, calls = task(), []
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def current_mode(self, *_): return 'green'
            def registration_unknown(self, identity, **values):
                calls.append(("unknown", identity, values)); return values["expected_version"] + 1
        class Resource:
            async def state(self): return {"boot_id": "boot-1", "adapter": "v1"}
            async def inspect(self, *_): return {"boot_id": "boot-1", "adapter": "v1", "nodes": [], "assets": []}
            async def register(self, *_): raise httpx.ReadTimeout("lost response")
            async def close(self): pass
        class Config:
            async def vision_ready(self, _): return True
            async def client(self, _): return Resource()
        runner = DrawingTaskRunner(Repo(), Config(), None, None, None, lambda _: None)
        async def build(_row): return PreparedExecution({"1": {}}, "a" * 64, [], [])
        runner._build_execution = build
        assert await runner.register(row['id']) == "unknown"
        assert calls[0][0] == "unknown" and calls[0][2]["boot_id"] == "boot-1"
    asyncio.run(run())


def test_registration_is_marked_unknown_before_an_interruptible_native_request():
    async def run():
        row, started = task(), asyncio.Event()
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def current_mode(self, *_): return "green"
            def registration_unknown(self, _id, *, expected_version, boot_id, execution_hash):
                row.update(resource_registration_state="unknown", boot_id=boot_id,
                           execution_hash=execution_hash, row_version=expected_version + 1)
                return row["row_version"]
        class Resource:
            async def state(self): return {"boot_id": "boot", "adapter": "v1"}
            async def inspect(self, *_): return {"boot_id": "boot", "adapter": "v1", "nodes": [], "assets": []}
            async def register(self, *_):
                started.set()
                await asyncio.Event().wait()
            async def close(self): pass
        class Config:
            async def vision_ready(self, _): return True
            async def client(self, _): return Resource()
        runner = DrawingTaskRunner(Repo(), Config(), None, None, None, lambda _: None)
        async def build(_row): return PreparedExecution({"1": {}}, "a" * 64, [], [])
        runner._build_execution = build
        pending = asyncio.create_task(runner.register(row["id"]))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert row["resource_registration_state"] == "unknown" and row["boot_id"] == "boot"
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        assert row["resource_registration_state"] == "unknown"
    asyncio.run(run())


def test_legacy_preset_registers_on_v6_only_with_the_original_dependency_signature():
    from al1s.adapters.postgres.meme_base import fingerprint
    async def run():
        row, calls = task(), []
        v5, v6 = "comfy-0.43-bb878b9-al1s-v5", "comfy-0.43-bb878b9-al1s-v6"
        nodes = [{"name": "CheckpointLoaderSimple", "sha256": "a" * 64}]
        signature = fingerprint({"adapter": v5, "nodes": nodes, "assets": []})
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def current_mode(self, *_): return "green"
            def registration_unknown(self, *_args, expected_version, **_kwargs): return expected_version + 1
            def mark_registration(self, *_args, **_kwargs): calls.append("registered")
        class Resource:
            async def state(self): return {"boot_id": "boot", "adapter": v6}
            async def inspect(self, *_): return {"boot_id": "boot", "adapter": v6, "nodes": nodes, "assets": []}
            async def register(self, *_): return {"state": "registered"}
            async def close(self): pass
        class Config:
            async def vision_ready(self, _): return True
            async def client(self, _): return Resource()
        runner = DrawingTaskRunner(Repo(), Config(), None, None, None, lambda _: None)
        async def build(_row):
            return PreparedExecution({"1": {}}, "a" * 64, ["CheckpointLoaderSimple"], [], signature,
                                     adapter_version=v5)
        runner._build_execution = build
        assert await runner.register(row["id"]) == "confirmed" and calls == ["registered"]
        nodes[0]["sha256"] = "b" * 64
        import pytest
        with pytest.raises(ValueError, match="drawing_dependency_changed"):
            await runner.register(row["id"])
    asyncio.run(run())


def test_lost_submission_is_reconciled_by_original_id_without_resubmission():
    async def run():
        row, calls = task(), []
        row.update(generation_status="submitting", resource_registration_state="confirmed", boot_id="boot-1",
                   execution_hash="a" * 64, row_version=4)
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def current_mode(self, *_): return 'green'
            def begin_submit(self, identity, expected_version):
                assert identity == row['id'] and expected_version == row['row_version']
                row.update(row_version=5, prompt_transport_started=True)
                return 5
            def transition(self, identity, before, after, expected_version, **_):
                assert before == row['generation_status'] and expected_version == row['row_version']
                row.update(generation_status=after, row_version=row['row_version'] + 1)
                calls.append(('transition', after))
                return row['row_version']
            def defer_check(self, identity, expected_version, seconds=2):
                assert expected_version == row['row_version']; calls.append(('defer', identity))
                row['row_version'] += 1; return row['row_version']
        class Resource:
            async def state(self): return {"boot_id": "boot-1", "adapter": "v1"}
            async def submit(self, prompt, *_args): calls.append(('submit', prompt)); return "unknown"
            async def query(self, prompt, *_args): calls.append(('query', prompt)); return None
            async def close(self): pass
        resource = Resource()
        class Config:
            async def client(self, _): return resource
        runner = DrawingTaskRunner(Repo(), Config(), None, None, None, lambda _: None)
        async def build(_row): return PreparedExecution({"1": {}}, "a" * 64, [], [])
        runner._build_execution = build
        await runner.advance(row['id'])
        assert row['generation_status'] == 'reconciling'
        await runner.advance(row['id'])
        assert [value for value in calls if value[0] == 'submit'] == [('submit', row['prompt_id'])]
        assert [value for value in calls if value[0] == 'query'] == [('query', row['prompt_id'])]
        assert not any(value[0] == 'release' for value in calls)
    asyncio.run(run())


def test_explicit_native_rejection_fails_and_releases_without_resubmission():
    async def run():
        row, calls = task(), []
        row.update(generation_status='submitting', resource_registration_state='confirmed', boot_id='boot-1',
                   execution_hash='a' * 64, row_version=4)
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def current_mode(self, *_): return 'green'
            def begin_submit(self, _id, expected_version):
                row.update(prompt_transport_started=True, row_version=expected_version + 1); return row['row_version']
            def transition(self, _id, before, after, expected_version, **values):
                assert row['generation_status'] == before and row['row_version'] == expected_version
                row.update(values, generation_status=after, row_version=expected_version + 1); return row['row_version']
            def set_result_ready(self, _id, expected_version, reply_kind):
                row.update(reply_kind=reply_kind, row_version=expected_version + 1); return row['row_version']
            def mark_cleanup(self, _id, expected_version, status):
                row.update(cleanup_status=status, row_version=expected_version + 1); return row['row_version']
        class Resource:
            async def state(self): return {'boot_id': 'boot-1', 'adapter': 'v1'}
            async def submit(self, *_): calls.append('submit'); return 'rejected'
            async def reject(self, *_): calls.append('reject'); return True
            async def release(self, *_): calls.append('release'); return 'retained'
            async def delete_history(self, *_): return True
            async def close(self): pass
        class Config:
            async def client(self, _): return Resource()
        runner = DrawingTaskRunner(Repo(), Config(), None, None, None, lambda _: None)
        async def build(_row): return PreparedExecution({'1': {}}, 'a' * 64, [], [])
        runner._build_execution = build
        assert await runner.advance(row['id']) == 'failed'
        assert row['generation_status'] == 'failed' and row['cleanup_status'] == 'done'
        assert calls == ['submit', 'reject', 'release']
    asyncio.run(run())


def test_green_block_records_failure_without_sending_the_generated_image():
    async def run():
        row, calls = task(), []
        row.update(operation='draw', generation_status='running', resource_registration_state='confirmed',
            prompt_transport_started=True, boot_id='boot-1', execution_hash='a' * 64, row_version=4,
            scope_kind='qq_group', target_id='123', sender_id='200', source_message_id='456', ai_version=1, drawing_version=1,
            audit_mode='green', preset_id=uuid4(), preset_revision=1)
        buffer = BytesIO(); Image.new('RGB', (8, 8), 'red').save(buffer, 'PNG'); body = buffer.getvalue()
        digest = hashlib.sha256(body).hexdigest()
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def current_mode(self, *_): return 'green'
            def set_stage(self, _id, _stage, expected_version):
                assert expected_version == row['row_version']
                row['row_version'] += 1
                return row['row_version']
            def transition(self, identity, before, after, expected_version, **values):
                assert expected_version == row['row_version'] and before == row['generation_status']
                row.update(values, generation_status=after, row_version=row['row_version'] + 1)
                return row['row_version']
            def reserve_output(self, identity, expected_version, size, checksum):
                assert expected_version == row['row_version'] and checksum == digest and size == len(body)
                row.update(output_sha256=checksum, output_bytes=size, row_version=row['row_version'] + 1)
                return row['row_version']
            def record_audit(self, identity, expected_version, decision):
                assert expected_version == row['row_version'] and decision.image_sha256 == digest
                row.update(audit_status=decision.status, row_version=row['row_version'] + 1)
                return row['row_version']
            def set_result_ready(self, identity, expected_version, reply_kind):
                assert expected_version == row['row_version']
                row.update(reply_kind=reply_kind, row_version=row['row_version'] + 1)
                return row['row_version']
            def mark_cleanup(self, identity, expected_version, status):
                assert expected_version == row['row_version']; row.update(cleanup_status=status, row_version=row['row_version'] + 1)
                return row['row_version']
        class Media:
            def put(self, identity, kind, value):
                assert kind == 'output' and value == body
                return f'al1s-drawing/{identity}/output.png', digest
        class Auditor:
            async def audit(self, value): return AuditDecision('block', hashlib.sha256(value).hexdigest())
        class Resource:
            async def download(self, *_): return body
            async def release(self, *_): calls.append('release'); return 'retained'
            async def delete_history(self, *_): calls.append('history'); return True
        runner = DrawingTaskRunner(Repo(), None, Media(), Auditor(), None, lambda _: None)
        await runner.complete(row['id'], {'id': str(row['prompt_id']), 'status': 'completed'}, Resource(), output_node='9')
        assert row['generation_status'] == 'succeeded' and row['reply_kind'] == 'failure_notice'
        assert row['cleanup_status'] == 'done' and calls == ['release', 'history']
    asyncio.run(run())


def test_green_reference_is_blocked_before_any_native_registration():
    async def run():
        row, calls = task(), []
        row.update(input_reference=f"al1s-drawing/{row['id']}/input.png", input_sha256="a" * 64,
            input_audit_status="pending", input_audit_sha256=None, input_audit_model_revision=None,
            input_audit_policy_version=None, scope_kind='qq_group', target_id='123', sender_id='200',
            source_message_id='456', ai_version=1, drawing_version=1, audit_mode='green')
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def current_mode(self, *_): return 'green'
            def set_stage(self, _id, _stage, expected_version):
                assert expected_version == row['row_version']
                row['row_version'] += 1
                return row['row_version']
            def record_input_audit(self, identity, expected_version, decision):
                assert expected_version == row['row_version']
                row.update(input_audit_status=decision.status, row_version=row['row_version'] + 1)
                return row['row_version']
            def transition(self, identity, before, after, expected_version, **_):
                assert before == row['generation_status'] and expected_version == row['row_version']
                row.update(generation_status=after, row_version=row['row_version'] + 1); return row['row_version']
            def set_result_ready(self, identity, expected_version, reply_kind):
                row.update(reply_kind=reply_kind, row_version=expected_version + 1); return row['row_version']
            def mark_cleanup(self, identity, expected_version, status):
                row.update(cleanup_status=status, row_version=expected_version + 1); return row['row_version']
        class Media:
            def read(self, *_args, **_kwargs): return b'image'
        class Auditor:
            async def audit(self, _): return AuditDecision('block', 'a' * 64)
        class Config:
            async def client(self, _): calls.append('client'); raise AssertionError('must not contact Comfy')
        runner = DrawingTaskRunner(Repo(), Config(), Media(), Auditor(), None, lambda _: None)
        assert await runner.register(row['id']) == 'blocked'
        assert row['generation_status'] == 'failed' and row['reply_kind'] == 'failure_notice'
        assert row['cleanup_status'] == 'done' and calls == []
    asyncio.run(run())


def test_request_admitted_as_r18_stops_if_group_turns_green_before_submit():
    async def run():
        for status in ('queued', 'submitting'):
            row = task()
            row.update(operation='draw', audit_mode='r18', generation_status=status,
                       resource_registration_state='confirmed' if status == 'submitting' else 'pending',
                       boot_id='boot-1' if status == 'submitting' else None)
            class Repo:
                def task_for_worker(self, _): return dict(row)
                def current_mode(self, *_): return 'green'
                def transition(self, _id, before, after, expected_version, **values):
                    assert row['generation_status'] == before and row['row_version'] == expected_version
                    row.update(values, generation_status=after, row_version=expected_version + 1)
                    return row['row_version']
                def set_result_ready(self, _id, expected_version, reply_kind):
                    row.update(reply_kind=reply_kind, row_version=expected_version + 1)
                    return row['row_version']
                def mark_cleanup(self, _id, expected_version, status):
                    row.update(cleanup_status=status, row_version=expected_version + 1)
                    return row['row_version']
            runner = DrawingTaskRunner(Repo(), None, None, None, None, lambda _: None)
            assert await runner._audit_reference(dict(row)) is None
            assert row['generation_status'] == 'failed' and row['error_code'] == 'drawing_mode_changed_retry'
            assert row['reply_kind'] == 'failure_notice'
            assert row['cleanup_status'] == ('done' if status == 'queued' else 'deferred')
    asyncio.run(run())


def test_unmanaged_local_vision_prevents_preview_or_drawing_registration():
    async def run():
        row, calls = task(), []
        class Repo:
            def task_for_worker(self, _): return dict(row)
            def current_mode(self, *_): return 'green'
            def defer_check(self, identity, expected_version, seconds):
                calls.append(('defer', identity, expected_version, seconds))
        class Config:
            async def vision_ready(self, _): return False
            async def client(self, _): raise AssertionError('must not register')
        runner = DrawingTaskRunner(Repo(), Config(), None, None, None, lambda _: None)
        assert await runner.register(row['id']) == 'waiting_for_vision'
        assert calls == [('defer', row['id'], 1, 5)]
    asyncio.run(run())


def test_pending_cleanup_does_not_block_already_prepared_delivery():
    async def run():
        delivered = asyncio.Event()
        class Repo:
            def expire_next(self, _): return None
            def next_cleanup(self, _): return uuid4()
            def next_registration(self, _, expected=None): return None
            def next_active(self, _): return None
            def claim_next(self, _): return None
            def next_delivery(self, _): return (uuid4(), 'result')
            def next_media_cleanup(self): return None
            def purge_records(self, **_): pass
        runner = DrawingTaskRunner(Repo(), None, None, None, None, lambda _: None)
        async def cleanup(_): raise httpx.ConnectError('offline')
        async def delivery(*_): delivered.set()
        runner.recover_cleanup, runner.deliver = cleanup, delivery
        runner.start()
        try:
            await asyncio.wait_for(delivered.wait(), timeout=1)
        finally:
            await runner.close()
    asyncio.run(run())
@pytest.mark.parametrize(("original", "positive", "expected"), [
    (None, "画一只猫", "png"),
    (None, "生成一只猫的 GIF 动图", "gif"),
    (None, "make a gif of a cat", "gif"),
    (None, "猫【负面】gif", "png"),
    (None, "画一张静态图，不要 GIF", "png"),
    (None, "画一张静态图，没有 GIF 动图", "png"),
    (None, "a still image without any animated GIF", "png"),
    ("画猫的动图【NP】水印", "cat", "gif"),
    ("画猫【NP】gif", "a cat gif", "png"),
    (None, "生成一段猫咪视频", "video"),
    (None, "一段小猫跳舞的视频", "video"),
    (None, "一张视频封面", "png"),
    (None, "帮我做一个视频的封面", "png"),
    (None, "生成一段猫咪视频或 GIF", "gif"),
])
def test_online_output_uses_explicit_original_positive_intent(original, positive, expected):
    from al1s.bot_drawing.runner import _online_output_format
    assert _online_output_format({"wish_text": original, "positive": positive}) == expected

