import asyncio

import pytest


def test_gpu_queue_orders_vision_and_drawing_without_preemption():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        queue = GpuQueue()
        queue.reserve("draw-1", owner="user-1")
        queue.reserve("vision-1", owner="user-2")
        queue.reserve("draw-2", owner="user-3")
        assert queue.active == "draw-1"
        waiter = asyncio.create_task(queue.wait("vision-1"))
        await asyncio.sleep(0)
        assert not waiter.done()
        queue.finish("draw-1")
        await waiter
        assert queue.active == "vision-1"
        queue.finish("vision-1")
        assert queue.active == "draw-2"

    asyncio.run(run())


def test_gpu_queue_accepts_multiple_tasks_from_same_user_and_bounds_total():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        queue = GpuQueue()
        queue.reserve("active", owner="same-user")
        queue.reserve("another", owner="same-user")
        for index in range(4):
            queue.reserve(f"wait-{index}", owner="same-user")
        with pytest.raises(ValueError, match="gpu_queue_full"):
            queue.reserve("overflow", owner="other")
        queue.finish("wait-2")
        queue.reserve("replacement", owner="other")
        assert queue.pending == 5

    asyncio.run(run())


def test_gpu_queue_recovery_preserves_existing_tasks_and_blocks_new_grants_until_ready():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        queue = GpuQueue(recovering=True)
        queue.reserve("new-vision", owner="new-user")
        assert queue.active is None
        queue.restore(active=("old-active", "old-user"), pending=[("old-pending", "old-user-2")])
        assert queue.active == "old-active"
        queue.finish("old-active")
        assert queue.active == "old-pending"
        queue.finish("old-pending")
        assert queue.active == "new-vision"

    asyncio.run(run())


def test_recovery_keeps_same_user_waiter_after_persisted_work():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        queue = GpuQueue(recovering=True)
        queue.reserve("new", owner="same-user")
        waiter = asyncio.create_task(queue.wait("new"))
        await asyncio.sleep(0)
        queue.restore(active=("old", "same-user"))
        assert queue.active == "old"
        assert not waiter.done()
        queue.finish("old")
        await waiter
        assert queue.active == "new"

    asyncio.run(run())


def test_recovery_keeps_persisted_tasks_and_rejects_only_excess_new_waiters():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        queue = GpuQueue(recovering=True)
        queue.reserve('new-1', owner='same-user')
        queue.reserve('new-2', owner='same-user')
        rejected = asyncio.create_task(queue.wait('new-2'))
        await asyncio.sleep(0)
        queue.restore(active=('old-active', 'same-user'),
                      pending=[(f'old-{index}', 'same-user') for index in range(4)])
        with pytest.raises(ValueError, match='gpu_queue_full'):
            await rejected
        assert queue.active == 'old-active' and queue.pending == 5
        for identity in ['old-active', 'old-0', 'old-1', 'old-2', 'old-3']:
            assert queue.active == identity
            queue.finish(identity)
        assert queue.active == 'new-1'

    asyncio.run(run())


def test_gpu_waiter_cancellation_does_not_grant_or_cancel_another_task():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        queue = GpuQueue()
        queue.reserve("current", owner="same-user")
        queue.reserve("cancelled", owner="same-user")
        queue.reserve("next", owner="same-user")
        waiting = asyncio.create_task(queue.wait("cancelled"))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert queue.active == "current"
        queue.finish("current")
        assert queue.active == "next"

    asyncio.run(run())


def test_same_user_drawing_waits_behind_vision_and_is_admitted():
    from uuid import uuid4
    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_drawing.service import DrawingService

    async def run():
        queue, service, task_id = GpuQueue(), uuid4(), uuid4()
        owner = (service, '123')
        queue.reserve('vision:existing', owner=owner)

        class Repo:
            def task_for_worker(self, _): return {'generation_status': 'queued', 'cleanup_status': 'none'}

        class Runner:
            gpu_queue = queue
            def wake(self): pass

        drawing = DrawingService(Repo(), None, None, None, Runner())
        assert await drawing._admit_queued(task_id, owner, lambda: task_id) == task_id
        assert queue.active == 'vision:existing' and queue.pending == 1
        queue.finish('vision:existing')
        assert queue.active == str(task_id)

    asyncio.run(run())


def test_new_drawing_waits_after_sent_result_until_prior_native_cleanup_confirms():
    from uuid import uuid4
    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_drawing.runner import DrawingTaskRunner
    from al1s.bot_drawing.service import DrawingService

    async def run():
        queue, service, old_id, next_id = GpuQueue(), uuid4(), uuid4(), uuid4()
        owner = (service, '123')
        queue.reserve(str(old_id), owner=owner)

        class Repo:
            def mark_cleanup(self, _id, *, expected_version, status): return expected_version + 1
            def task_for_worker(self, _): return {'generation_status': 'queued', 'cleanup_status': 'none'}

        class Resource:
            status = 'deferred'
            async def release(self, *_): return self.status
            async def delete_history(self, *_): return True

        repo, resource = Repo(), Resource()
        runner = DrawingTaskRunner(repo, None, None, None, None, None, gpu_queue=queue)
        drawing = DrawingService(repo, None, None, None, runner)
        old = {'id': old_id, 'service_id': service, 'prompt_id': uuid4(), 'operation': 'draw',
               'execution_hash': 'a' * 64, 'send_status': 'sent'}
        _, cleaned = await runner._release(old, resource, 1)
        assert not cleaned
        assert await drawing._admit_queued(next_id, owner, lambda: next_id) == next_id
        assert queue.active == str(old_id) and queue.pending == 1
        resource.status = 'retained'
        _, cleaned = await runner._release(old, resource, 2)
        assert cleaned and queue.active == str(next_id)

    asyncio.run(run())


def test_disabling_vision_cancels_only_that_connections_waiting_vision_requests():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        queue = GpuQueue()
        queue.reserve("drawing", owner=("service", "1"))
        queue.reserve("vision:cancel", owner=("service", "2"))
        queue.reserve("vision:keep", owner=("other", "3"))
        waiter = asyncio.create_task(queue.wait("vision:cancel"))
        await asyncio.sleep(0)
        queue.cancel_waiting_vision("service")
        with pytest.raises(ValueError, match="gpu_vision_disabled"):
            await waiter
        assert queue.active == "drawing" and queue.pending == 1
        queue.finish("drawing")
        assert queue.active == "vision:keep"
    asyncio.run(run())


def test_preview_reserves_gpu_before_persisting_and_releases_on_admission_failure():
    from types import SimpleNamespace
    from uuid import uuid4

    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_drawing.service import DrawingService

    async def run():
        queue, service_id, preset_id = GpuQueue(), uuid4(), uuid4()
        class Repo:
            failed = False
            def get_preset(self, *_): return {"row_version": 1, "kind": "txt2img"}
            def task_for_worker(self, _): return {"generation_status": "queued", "cleanup_status": "none"}
            def admit_preview(self, *_args, **kwargs):
                if self.failed:
                    raise ValueError("drawing_route_changed")
                return kwargs["task_id"]
        class Runner:
            gpu_queue = queue
            def wake(self): pass
        repo = Repo()
        service = DrawingService(repo, None, None, None, Runner())
        result = await service.submit_preview(service_id, preset_id, positive="test", negative="",
                                              mode="green", image=None, expected_version=1, operation_id="one")
        assert queue.active == str(result)
        repo.failed = True
        with pytest.raises(ValueError, match="drawing_route_changed"):
            await service.submit_preview(service_id, preset_id, positive="test", negative="", mode="green",
                                         image=None, expected_version=1, operation_id="two")
        assert queue.active == str(result) and queue.pending == 0

    asyncio.run(run())


def test_replayed_completed_preview_does_not_block_the_gpu_queue():
    from uuid import uuid4
    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_drawing.service import DrawingService

    async def run():
        queue, service_id, preset_id = GpuQueue(), uuid4(), uuid4()
        class Repo:
            def get_preset(self, *_): return {"row_version": 1, "kind": "txt2img"}
            def admit_preview(self, *_args, **kwargs): return kwargs["task_id"]
            def task_for_worker(self, _): return {"generation_status": "succeeded", "cleanup_status": "done"}
        class Runner:
            gpu_queue = queue
            def wake(self): pass
        service = DrawingService(Repo(), None, None, None, Runner())
        await service.submit_preview(service_id, preset_id, positive="test", negative="", mode="green",
                                     image=None, expected_version=1, operation_id="replayed")
        assert queue.idle
    asyncio.run(run())


def test_drawing_admission_waits_for_recovery_before_persisting_a_new_task():
    from uuid import uuid4
    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_drawing.service import DrawingService

    async def run():
        queue, service_id, preset_id, calls = GpuQueue(recovering=True), uuid4(), uuid4(), []
        class Repo:
            def get_preset(self, *_): return {"row_version": 1, "kind": "txt2img"}
            def admit_preview(self, *_args, **kwargs): calls.append("admitted"); return kwargs["task_id"]
            def task_for_worker(self, _): return {"generation_status": "queued", "cleanup_status": "none"}
        class Runner:
            gpu_queue = queue
            def wake(self): pass
        service = DrawingService(Repo(), None, None, None, Runner())
        task = asyncio.create_task(service.submit_preview(service_id, preset_id, positive="test", negative="",
            mode="green", image=None, expected_version=1, operation_id="new"))
        await asyncio.sleep(0.02)
        assert not calls
        queue.restore(active=("old", None))
        await task
        assert calls == ["admitted"] and queue.active == "old" and queue.pending == 1
    asyncio.run(run())


def test_waiting_image_sessions_do_not_occupy_plain_text_scheduler_slots():
    from al1s.meme_workshop.contracts import CommandSource
    from al1s.meme_workshop.qq_scheduler import QqScheduler

    async def run():
        scheduler, release = QqScheduler(active_limit=1, vision_limit=2), asyncio.Event()
        started = []
        def source(message, group):
            return CommandSource(message_id=message, scope_kind="qq_group", target_id=group, sender_id="1")
        async def wait_for_gpu(label):
            started.append(label)
            await release.wait()
        async def plain_text(): started.append("text")
        scheduler.try_submit("ai_vision", ("service", "group-1"), source("1", "1"), lambda: wait_for_gpu("v1"))
        scheduler.try_submit("ai_vision", ("service", "group-2"), source("2", "2"), lambda: wait_for_gpu("v2"))
        scheduler.try_submit("ai", ("service", "group-3"), source("3", "3"), plain_text)
        await asyncio.sleep(0)
        assert started == ["v1", "v2", "text"]
        release.set()
        await scheduler.drain()
        await scheduler.stop()

    asyncio.run(run())


def test_seventh_ready_vision_request_is_rejected_when_gpu_queue_is_full():
    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.meme_workshop.contracts import CommandSource
    from al1s.meme_workshop.qq_scheduler import QqScheduler

    async def run():
        gpu, scheduler, release, result = GpuQueue(), QqScheduler(), asyncio.Event(), []
        async def vision(index):
            try:
                gpu.reserve(str(index), owner=str(index))
            except ValueError as error:
                result.append(str(error))
                return
            await release.wait()
        for index in range(1, 8):
            source = CommandSource(message_id=str(index), scope_kind="qq_group", target_id=str(index), sender_id=str(index))
            scheduler.try_submit("ai_vision", ("service", str(index)), source, lambda index=index: vision(index))
        await asyncio.sleep(0.02)
        assert result == ["gpu_queue_full"]
        release.set()
        await scheduler.drain()
        await scheduler.stop()
    asyncio.run(run())


def test_disabling_vision_removes_queued_image_sessions_without_dropping_text():
    from al1s.meme_workshop.contracts import CommandSource
    from al1s.meme_workshop.qq_scheduler import QqScheduler

    async def run():
        scheduler, release = QqScheduler(active_limit=1, vision_limit=1), asyncio.Event()
        def source(message, group):
            return CommandSource(message_id=message, scope_kind="qq_group", target_id=group, sender_id="1")
        async def hold(): await release.wait()
        scheduler.try_submit("ai_vision", ("s", "one"), source("1", "1"), hold)
        scheduler.try_submit("ai_vision", ("s", "two"), source("2", "2"), hold)
        scheduler.try_submit("ai", ("s", "three"), source("3", "3"), hold)
        dropped = scheduler.drop_vision("s")
        assert [item.source.message_id for item in dropped] == ["2"]
        assert scheduler.active(("s", "one")) and scheduler.active(("s", "three"))
        release.set()
        await scheduler.drain()
        await scheduler.stop()
    asyncio.run(run())


def test_only_local_vision_requests_receive_the_separate_gpu_wait_budget():
    from types import SimpleNamespace
    from al1s.bot_ai.service import _local_vision, _request_deadline

    settings = SimpleNamespace(vision_enabled=True,
        providers={"vision": SimpleNamespace(adapter_kind="al1s_comfyui_vision")})
    image = SimpleNamespace(parts=[{"type": "image"}])
    text = SimpleNamespace(parts=[{"type": "text"}])
    assert _local_vision(settings, image) is True
    assert _local_vision(settings, text) is False
    assert (_request_deadline(settings, image) - _request_deadline(settings, text)).total_seconds() >= 1800
    settings.providers["vision"].adapter_kind = "follow_chat"
    assert _local_vision(settings, image) is False
