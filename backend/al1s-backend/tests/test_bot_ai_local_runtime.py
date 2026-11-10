import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from al1s.bot_ai.contracts import AiEnvelope
from al1s.bot_ai.local_runtime import LocalAiRuntime
from al1s.meme_workshop.contracts import CommandSource


def envelope(service):
    return AiEnvelope(request_id=uuid4(), service_id=service, config_version=1,
        source=CommandSource(message_id='1', scope_kind='qq_group', target_id='100', sender_id='200'),
        self_id='300', sender_name='测试', parts=[{'type': 'text', 'text': 'hello'}],
        deadline=datetime.now(UTC) + timedelta(seconds=30))


def test_tool_replay_is_idempotent_and_output_is_owned_by_request():
    async def run():
        service, calls, sent, completed = uuid4(), [], [], []
        done = asyncio.Event()
        async def on_tool(item, call_id, name, arguments):
            calls.append((item.request_id, call_id, name, arguments))
            return {'status': 'ok'}
        async def on_output(item, sequence, parts):
            sent.append((item.request_id, sequence, parts))
            return {'status': 'sent'}
        async def on_completed(item, status, *rest):
            completed.append((item.request_id, status, rest))
            done.set()
        class Executor:
            async def run(self, item, invocation):
                first = await invocation.tool('one', 'search_feeds', {'query': 'x'})
                again = await invocation.tool('one', 'search_feeds', {'query': 'x'})
                assert first == again == {'status': 'ok'}
                await invocation.output(0, [{'type': 'text', 'text': 'ok'}])
                return 'completed', None
        runtime = LocalAiRuntime()
        runtime.set_handlers(on_tool=on_tool, on_output=on_output, on_completed=on_completed)
        await runtime.activate(service, 1, Executor())
        item = envelope(service)
        await runtime.dispatch(item)
        await asyncio.wait_for(done.wait(), 2)
        assert len(calls) == len(sent) == 1
        assert calls[0][0] == sent[0][0] == completed[0][0] == item.request_id
        assert completed[0][1] == 'completed'
        await runtime.close()
    asyncio.run(run())


def test_gpu_queue_rejection_reaches_the_ai_service_as_a_bounded_error_code():
    async def run():
        service, done, results = uuid4(), asyncio.Event(), []
        async def on_completed(_item, status, code=None):
            results.append((status, code))
            done.set()
        class Executor:
            async def run(self, *_): raise ValueError('gpu_queue_full')
        runtime = LocalAiRuntime()
        runtime.set_handlers(on_completed=on_completed)
        await runtime.activate(service, 1, Executor())
        await runtime.dispatch(envelope(service))
        await asyncio.wait_for(done.wait(), 2)
        assert results == [('failed', 'gpu_queue_full')]
        await runtime.close()
    asyncio.run(run())


def test_cancellation_revokes_late_output_and_waits_for_inflight_tool():
    async def run():
        service, sends, completed = uuid4(), [], []
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def on_tool(_item, _id, _name, _arguments):
            started.set()
            await release.wait()
            return {'status': 'ok'}
        async def on_output(_item, _sequence, parts):
            sends.append(parts)
            return {'status': 'sent'}
        async def on_completed(_item, status, *rest):
            completed.append(status)
            finished.set()
        class Executor:
            async def run(self, item, invocation):
                try:
                    await invocation.tool('one', 'search_feeds', {})
                except asyncio.CancelledError:
                    assert (await invocation.output(0, [{'type': 'text', 'text': 'late'}]))['status'] == 'denied'
                return 'completed', None
        runtime = LocalAiRuntime()
        runtime.set_handlers(on_tool=on_tool, on_output=on_output, on_completed=on_completed)
        await runtime.activate(service, 1, Executor())
        item = envelope(service)
        await runtime.dispatch(item)
        await asyncio.wait_for(started.wait(), 2)
        runtime.revoke(item.request_id)
        await runtime.cancel(item.request_id)
        await asyncio.sleep(0)
        await runtime.cancel(item.request_id)
        await asyncio.sleep(0)
        assert not finished.is_set() and not sends
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        assert completed == ['interrupted'] and not sends
        await runtime.close()
    asyncio.run(run())


def test_failed_tool_replay_returns_same_result_without_executing_twice():
    async def run():
        service, calls, statuses = uuid4(), [], []
        done = asyncio.Event()
        async def on_tool(*_args):
            calls.append(1)
            raise ValueError('failed')
        async def on_completed(_item, status, *rest):
            statuses.append(status)
            done.set()
        class Executor:
            async def run(self, _item, invocation):
                first = await invocation.tool('one', 'search_feeds', {})
                second = await invocation.tool('one', 'search_feeds', {})
                assert first == second == {'status': 'failed', 'code': 'ai_effect_failed'}
                return 'completed', None
        runtime = LocalAiRuntime()
        runtime.set_handlers(on_tool=on_tool, on_completed=on_completed)
        await runtime.activate(service, 1, Executor())
        await runtime.dispatch(envelope(service))
        await asyncio.wait_for(done.wait(), 2)
        assert len(calls) == 1 and statuses == ['completed']
        await runtime.close()
    asyncio.run(run())


def test_staged_sticker_guard_stays_bound_to_its_request():
    async def run():
        service, done, statuses = uuid4(), asyncio.Event(), []
        runtime = LocalAiRuntime()
        async def on_output(item, _sequence, _parts):
            allowed, fallback = runtime.output_guard(item.request_id)
            assert not allowed('/request/sticker.gif') and allowed('/request/chart.png')
            assert fallback == '文字兜底'
            return {'status': 'sent'}
        async def on_completed(_item, status, *rest):
            statuses.append(status)
            done.set()
        class Executor:
            async def run(self, _item, invocation):
                invocation.guard_image('/request/sticker.gif', lambda: False, '文字兜底')
                await invocation.output(0, [{'type':'text','text':'ok'}])
                return 'completed', None
        runtime.set_handlers(on_output=on_output, on_completed=on_completed)
        await runtime.activate(service, 1, Executor())
        await runtime.dispatch(envelope(service))
        await asyncio.wait_for(done.wait(), 2)
        assert statuses == ['completed']
        await runtime.close()
    asyncio.run(run())


def test_cancel_waits_for_inflight_blocking_database_work():
    async def run():
        import threading
        service, done = uuid4(), asyncio.Event()
        started, release = threading.Event(), threading.Event()
        statuses = []
        def blocking_work():
            started.set()
            assert release.wait(3)
        async def on_completed(_item, status, *rest):
            statuses.append(status)
            done.set()
        class Executor:
            async def run(self, _item, invocation):
                await invocation.blocking(blocking_work)
                return 'completed', None
        runtime = LocalAiRuntime()
        runtime.set_handlers(on_completed=on_completed)
        await runtime.activate(service, 1, Executor())
        item = envelope(service)
        await runtime.dispatch(item)
        assert await asyncio.to_thread(started.wait, 2)
        await runtime.cancel(item.request_id)
        await asyncio.sleep(0)
        assert not done.is_set()
        release.set()
        await asyncio.wait_for(done.wait(), 2)
        assert statuses == ['interrupted']
        await runtime.close()
    asyncio.run(run())
