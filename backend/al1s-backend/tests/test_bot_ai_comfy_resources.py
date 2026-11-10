import asyncio
from uuid import uuid4

import httpx
import pytest

from al1s.bot_ai.managed.comfy_client import ComfyWorkflowClient, managed_vision_workflow


def test_managed_vision_graph_uses_protected_qwen_and_image_nodes():
    prompt = uuid4()
    graph, digest = managed_vision_workflow("描述图片", "data:image/png;base64,YQ==", 512, prompt)
    assert graph["1"]["class_type"] == "AL1SImageReceiver"
    assert graph["2"]["class_type"] == "AL1SQwenVLAdvanced"
    assert graph["1"]["inputs"]["al1s_prompt_id"] == str(prompt)
    assert graph["2"]["inputs"]["keep_model_loaded"] is True and len(digest) == 64


@pytest.mark.parametrize('native_state', ['missing', 'missing_busy', 'cancelled', 'running', 'foreign_cancelled'])
def test_old_vision_job_after_comfy_restarts_is_cleared_only_after_native_confirmation(native_state):
    async def run():
        service, prompt, calls = uuid4(), uuid4(), []
        pending = [{'prompt_id': str(prompt), 'created_at': 1.0, 'boot_id': 'old-boot',
                    'execution_hash': 'a' * 64, 'registration_state': 'confirmed', 'transport_started': True}]

        async def load(): return list(pending)
        async def save(rows): pending[:] = rows

        class Resource:
            async def state(self): return {'boot_id': 'new-boot'}
            async def query(self, *_): raise AssertionError('old boot has no current registration')
            async def release(self, *_): raise AssertionError('must not release a different boot')

        def handler(request):
            calls.append((request.method, request.url.path))
            if request.url.path == f'/api/jobs/{prompt}':
                if native_state in {'missing', 'missing_busy'}: return httpx.Response(404)
                status = 'cancelled' if native_state == 'foreign_cancelled' else native_state
                owner = 'another-owner' if native_state == 'foreign_cancelled' else 'owner'
                return httpx.Response(200, json={'id': str(prompt), 'status': status,
                    'workflow': {'extra_data': {'client_id': owner}}})
            if request.url.path == '/queue':
                return httpx.Response(200, json={'queue_running': [[1]] if native_state == 'missing_busy' else [],
                                                 'queue_pending': []})
            if request.url.path == '/history': return httpx.Response(200, json={})
            raise AssertionError(request.url.path)

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='http://localhost:8188')
        client = ComfyWorkflowClient('http://localhost:8188', 'owner', load, save, client=http,
                                     resource=Resource(), service_id=service, config_version=1)
        if native_state == 'foreign_cancelled':
            with pytest.raises(ValueError, match='comfy_job_owner_mismatch'):
                await client.cleanup()
            assert len(pending) == 1 and ('POST', '/history') not in calls
            await client.close()
            return
        remaining = await client.cleanup()
        assert bool(remaining) is (native_state in {'running', 'missing_busy'})
        assert bool(pending) is (native_state in {'running', 'missing_busy'})
        assert (('POST', '/history') in calls) is (native_state == 'cancelled')
        await client.close()

    asyncio.run(run())


def test_vision_waits_behind_drawing_without_spending_its_execution_timeout():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        service, pending, calls = uuid4(), [], []
        queue = GpuQueue()
        queue.reserve("drawing", owner="another")
        async def load(): return list(pending)
        async def save(rows): pending[:] = rows
        class Resource:
            async def switch(self, operation, profile): calls.append("switch"); return "ready"
            async def state(self): return {"boot_id": "boot"}
            async def set_pin(self, *_): pass
            async def register(self, *_): return {"state": "registered"}
            async def submit(self, *_): calls.append("submit"); return "accepted"
            async def query(self, prompt, *_):
                return {"id": str(prompt), "status": "completed", "outputs": {"3": {"text": ["猫"]}},
                        "workflow": {"extra_data": {"client_id": "owner"}}}
            async def release(self, *_): return "retained"
        def handler(request):
            if request.url.path == "/system_stats":
                return httpx.Response(200, json={"system": {"comfyui_version": "0.43.0"}})
            if request.url.path == "/history": return httpx.Response(200, json={})
            raise AssertionError(request.url.path)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = ComfyWorkflowClient("http://localhost:8188", "owner", load, save, client=http,
                                     resource=Resource(), service_id=service, config_version=1,
                                     timeout=0.03, gpu_queue=queue)
        job = asyncio.create_task(client.generate("描述图片"))
        await asyncio.sleep(0.08)
        assert not job.done() and calls == []
        queue.finish("drawing")
        assert await job == "猫"
        assert calls == ["switch", "submit"] and queue.idle
        await client.close()
    asyncio.run(run())


def test_cancelled_vision_keeps_gpu_turn_until_its_native_result_is_confirmed():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        service, pending, queue, submitted = uuid4(), [], GpuQueue(), asyncio.Event()
        async def load(): return list(pending)
        async def save(rows): pending[:] = rows
        class Resource:
            terminal = False
            async def switch(self, *_): return "ready"
            async def state(self): return {"boot_id": "boot"}
            async def set_pin(self, *_): pass
            async def register(self, *_): return {"state": "registered"}
            async def submit(self, *_): submitted.set(); return "unknown"
            async def query(self, prompt, *_):
                if not self.terminal: return None
                return {"id": str(prompt), "status": "failed", "workflow": {"extra_data": {"client_id": "owner"}}}
            async def release(self, *_): return "retained"
        resource = Resource()
        def handler(request):
            if request.url.path == "/system_stats":
                return httpx.Response(200, json={"system": {"comfyui_version": "0.43.0"}})
            if request.url.path == "/history": return httpx.Response(200, json={})
            raise AssertionError(request.url.path)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = ComfyWorkflowClient("http://localhost:8188", "owner", load, save, client=http,
                                     resource=resource, service_id=service, config_version=1, gpu_queue=queue)
        task = asyncio.create_task(client.generate("describe"))
        await submitted.wait()
        queue.reserve("next-draw")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert queue.active != "next-draw" and len(pending) == 1
        resource.terminal = True
        assert await client.cleanup() == []
        assert queue.active == "next-draw"
        await client.close()
    asyncio.run(run())


def test_confirmed_drawing_cleanup_releases_the_same_queue_for_waiting_vision():
    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_drawing.runner import DrawingTaskRunner

    async def run():
        service, drawing, queue, pending = uuid4(), uuid4(), GpuQueue(), []
        queue.reserve(str(drawing), owner=(service, "1"))
        async def load(): return list(pending)
        async def save(rows): pending[:] = rows
        class Resource:
            async def switch(self, *_): return "ready"
            async def state(self): return {"boot_id": "boot"}
            async def set_pin(self, *_): pass
            async def register(self, *_): return {"state": "registered"}
            async def submit(self, *_): return "accepted"
            async def query(self, prompt, *_):
                return {"id": str(prompt), "status": "completed", "outputs": {"3": {"text": ["ok"]}},
                        "workflow": {"extra_data": {"client_id": "owner"}}}
            async def release(self, *_): return "retained"
            async def delete_history(self, *_): return True
        resource = Resource()
        def handler(request):
            if request.url.path == "/system_stats":
                return httpx.Response(200, json={"system": {"comfyui_version": "0.43.0"}})
            if request.url.path == "/history": return httpx.Response(200, json={})
            raise AssertionError(request.url.path)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = ComfyWorkflowClient("http://localhost:8188", "owner", load, save, client=http,
                                     resource=resource, service_id=service, config_version=1, gpu_queue=queue)
        task = asyncio.create_task(client.generate("describe"))
        await asyncio.sleep(0.02)
        assert not task.done() and queue.pending == 1
        class Repo:
            def mark_cleanup(self, _id, expected_version, status): return expected_version + 1
        runner = DrawingTaskRunner(Repo(), None, None, None, None, None, gpu_queue=queue)
        row = {"id": drawing, "prompt_id": uuid4(), "service_id": service, "operation": "draw",
               "execution_hash": "a" * 64}
        assert (await runner._release(row, resource, 1))[1]
        assert await task == "ok" and queue.idle
        await client.close()
    asyncio.run(run())


def test_cleanup_does_not_release_a_new_vision_turn_while_it_is_switching_models():
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        queue, service, pending, switching = GpuQueue(), uuid4(), [], asyncio.Event()
        async def load(): return list(pending)
        async def save(rows): pending[:] = rows
        class Resource:
            async def switch(self, *_): switching.set(); await asyncio.Event().wait()
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                                 base_url="http://localhost:8188")
        client = ComfyWorkflowClient("http://localhost:8188", "owner", load, save, client=http,
                                     resource=Resource(), service_id=service, config_version=1, gpu_queue=queue)
        task = asyncio.create_task(client.generate("describe"))
        await switching.wait()
        ticket = queue.active
        assert await client.cleanup() == [] and queue.active == ticket
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert queue.idle
        await client.close()
    asyncio.run(run())


def test_disabling_vision_cancels_a_preparing_request_before_native_submission():
    from al1s.bot_ai.gpu_queue import GpuQueue, gpu_owner
    import pytest

    async def run():
        queue, service, pending, switching = GpuQueue(), uuid4(), [], asyncio.Event()
        async def load(): return list(pending)
        async def save(rows): pending[:] = rows
        class Resource:
            async def switch(self, *_): switching.set(); return "deferred"
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                                 base_url="http://localhost:8188")
        client = ComfyWorkflowClient("http://localhost:8188", "owner", load, save, client=http,
                                     resource=Resource(), service_id=service, config_version=1, gpu_queue=queue)
        token = gpu_owner.set((service, "sender"))
        try:
            job = asyncio.create_task(client.generate("describe"))
        finally:
            gpu_owner.reset(token)
        await switching.wait()
        queue.cancel_waiting_vision(service)
        with pytest.raises(ValueError, match="gpu_vision_disabled"):
            await asyncio.wait_for(job, 2)
        assert queue.idle and pending == []
        await client.close()
    asyncio.run(run())


def test_disabling_vision_removes_a_native_queued_request_without_interrupting_running_work():
    from al1s.bot_ai.gpu_queue import GpuQueue, gpu_owner
    import pytest

    async def run():
        queue, service, pending, submitted, calls = GpuQueue(), uuid4(), [], asyncio.Event(), []
        async def load(): return list(pending)
        async def save(rows): pending[:] = rows
        class Resource:
            async def switch(self, *_): return "ready"
            async def state(self): return {"boot_id": "boot"}
            async def set_pin(self, *_): pass
            async def register(self, *_): return {"state": "registered"}
            async def submit(self, *_): submitted.set(); return "accepted"
            async def query(self, prompt, *_): return {"id": str(prompt), "status": "pending"}
            async def cancel_queued(self, *_): calls.append("cancel_queued"); return "cancelled"
            async def release(self, *_): calls.append("release"); return "retained"
        def handler(request):
            if request.url.path == "/system_stats":
                return httpx.Response(200, json={"system": {"comfyui_version": "0.43.0"}})
            raise AssertionError(request.url.path)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = ComfyWorkflowClient("http://localhost:8188", "owner", load, save, client=http,
                                     resource=Resource(), service_id=service, config_version=1, gpu_queue=queue)
        token = gpu_owner.set((service, "sender"))
        try:
            job = asyncio.create_task(client.generate("describe"))
        finally:
            gpu_owner.reset(token)
        await submitted.wait()
        queue.cancel_waiting_vision(service)
        with pytest.raises(ValueError, match="gpu_vision_disabled"):
            await asyncio.wait_for(job, 2)
        assert calls == ["cancel_queued", "release"] and pending == [] and queue.idle
        await client.close()
    asyncio.run(run())


def test_disabling_vision_does_not_interrupt_a_job_that_started_during_cancellation():
    from al1s.bot_ai.gpu_queue import GpuQueue, gpu_owner

    async def run():
        queue, service, pending, submitted, calls = GpuQueue(), uuid4(), [], asyncio.Event(), []
        proceed = asyncio.Event()
        async def load(): return list(pending)
        async def save(rows): pending[:] = rows
        class Resource:
            count = 0
            async def switch(self, *_): return "ready"
            async def state(self): return {"boot_id": "boot"}
            async def set_pin(self, *_): pass
            async def register(self, *_): return {"state": "registered"}
            async def submit(self, *_): submitted.set(); return "accepted"
            async def query(self, prompt, *_):
                self.count += 1
                if self.count == 1:
                    await proceed.wait()
                    return {"id": str(prompt), "status": "pending"}
                return {"id": str(prompt), "status": "completed", "outputs": {"3": {"text": ["ok"]}},
                        "workflow": {"extra_data": {"client_id": "owner"}}}
            async def cancel_queued(self, *_): calls.append("checked"); return "running"
            async def release(self, *_): return "retained"
        resource = Resource()
        def handler(request):
            if request.url.path == "/system_stats":
                return httpx.Response(200, json={"system": {"comfyui_version": "0.43.0"}})
            if request.url.path == "/history": return httpx.Response(200, json={})
            raise AssertionError(request.url.path)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = ComfyWorkflowClient("http://localhost:8188", "owner", load, save, client=http,
                                     resource=resource, service_id=service, config_version=1, gpu_queue=queue)
        token = gpu_owner.set((service, "sender"))
        try:
            job = asyncio.create_task(client.generate("describe"))
        finally:
            gpu_owner.reset(token)
        await submitted.wait()
        queue.cancel_waiting_vision(service)
        proceed.set()
        assert await asyncio.wait_for(job, 2) == "ok"
        assert calls == ["checked"] and queue.idle
        await client.close()
    asyncio.run(run())


def test_managed_vision_persists_transport_before_submission_and_recovers_lost_reply():
    async def run():
        service, pending, calls = uuid4(), [], []
        async def load(): return list(pending)
        async def save(rows): pending[:] = rows
        class Resource:
            async def state(self): return {"boot_id": "boot-1"}
            async def set_pin(self, service_id, version, enabled):
                calls.append(("pin", service_id, version, enabled))
            async def register(self, prompt, service_id, operation, digest, boot, assets):
                calls.append(("register", prompt))
                return {"state": "registered"}
            async def submit(self, prompt, service_id, operation, digest, graph):
                assert pending[0]["transport_started"] is True and pending[0]["execution_hash"] == digest
                calls.append(("submit", prompt))
                return "unknown"
            async def query(self, prompt, service_id, operation, digest):
                return {"id": str(prompt), "status": "completed", "outputs": {"3": {"text": ["一只猫"]}},
                        "workflow": {"extra_data": {"client_id": "owner"}}}
            async def release(self, *_args): return "retained"
            async def cancel(self, *_args): raise AssertionError("completed job must not be cancelled")
        def handler(request):
            if request.url.path == "/system_stats":
                return httpx.Response(200, json={"system": {"comfyui_version": "0.43.0"}})
            if request.url.path == "/history":
                return httpx.Response(200, json={})
            raise AssertionError(request.url.path)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = ComfyWorkflowClient("http://localhost:8188", "owner", load, save, client=http,
                                     resource=Resource(), service_id=service, config_version=1)
        assert await client.generate("描述图片") == "一只猫"
        assert len([value for value in calls if value[0] == "submit"]) == 1 and pending == []
        await client.close()
    asyncio.run(run())


def test_managed_missing_job_remains_pending_instead_of_assuming_no_submission():
    async def run():
        service = uuid4()
        pending = [{"prompt_id": str(uuid4()), "created_at": 1, "boot_id": "boot-1",
                    "execution_hash": "a" * 64, "registration_state": "confirmed", "transport_started": True}]
        async def load(): return list(pending)
        async def save(rows): pending[:] = rows
        class Resource:
            async def state(self): return {'boot_id': 'boot-1'}
            async def query(self, *_args): return None
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                                 base_url="http://localhost:8188")
        client = ComfyWorkflowClient("http://localhost:8188", "owner", load, save, client=http,
                                     resource=Resource(), service_id=service, config_version=1)
        assert len(await client.cleanup()) == 1 and len(pending) == 1
        await client.close()
    asyncio.run(run())


def test_vision_protection_requires_active_matching_managed_client_and_reapplies_pin():
    from types import SimpleNamespace
    from al1s.bot_ai.model_manager import LocalModelManager
    async def run():
        service, calls = uuid4(), []
        manager = LocalModelManager(None, None, None, None)
        assert not await manager.protect_vision(service, 7)
        manager._active[service] = SimpleNamespace(version=7, comfy=SimpleNamespace(resource=None))
        assert not await manager.protect_vision(service, 7)
        class Resource:
            async def set_pin(self, *args): calls.append(args)
        manager._active[service].comfy.resource = Resource()
        assert not await manager.protect_vision(service, 8)
        assert await manager.protect_vision(service, 7)
        assert calls == [(service, 7, True)]
    asyncio.run(run())


def test_model_manager_passes_shared_gpu_queue_to_managed_vision_client():
    from types import SimpleNamespace
    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_ai.model_manager import LocalModelManager

    async def run():
        queue, service = GpuQueue(), uuid4()
        class History:
            def vision_jobs(self, *_): return []
        manager = LocalModelManager(None, None, History(), None, gpu_queue=queue)
        provider = SimpleNamespace(base_url="http://localhost:8188")
        resource = SimpleNamespace()
        client = manager._comfy(service, provider, resource=resource, version=1)
        assert client.gpu_queue is queue
        await client.close()
    asyncio.run(run())


def test_vision_prewarm_waits_until_drawing_models_are_released_and_queue_is_idle():
    from types import SimpleNamespace
    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_ai.model_manager import LocalModelManager

    async def run():
        queue, service, calls = GpuQueue(), uuid4(), []
        class Resource:
            drawing = 1
            async def state(self): return {"vision_pins": 1, "vision_resources": 0,
                                           "drawing_resources": self.drawing, "active": 0}
            async def switch(self, *_): return "ready"
        class Comfy:
            resource = Resource()
            async def generate(self, prompt, **kwargs): calls.append((prompt, kwargs)); return "ok"
        snapshot = SimpleNamespace(comfy=Comfy())
        manager = LocalModelManager(None, None, None, None, gpu_queue=queue)
        manager._active[service] = snapshot
        assert not await manager._warm_vision_once(service, snapshot)
        snapshot.comfy.resource.drawing = 0
        queue.reserve("queued-drawing")
        assert not await manager._warm_vision_once(service, snapshot)
        queue.finish("queued-drawing")
        assert await manager._warm_vision_once(service, snapshot)
        assert calls[0][1]["max_tokens"] == 64
    asyncio.run(run())


def test_unsubmitted_managed_vision_job_releases_its_registration_on_recovery():
    async def run():
        service, prompt, calls = uuid4(), uuid4(), []
        pending = [{'prompt_id': str(prompt), 'created_at': 1, 'boot_id': 'boot-1',
                    'execution_hash': 'a' * 64, 'registration_state': 'pending', 'transport_started': False}]
        async def load(): return list(pending)
        async def save(rows): pending[:] = rows
        class Resource:
            async def state(self): return {'boot_id': 'boot-1'}
            async def register(self, *_): calls.append('register'); return {'state': 'registered'}
            async def release(self, *_): calls.append('release'); return 'retained'
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                                 base_url='http://localhost:8188')
        client = ComfyWorkflowClient('http://localhost:8188', 'owner', load, save, client=http,
                                     resource=Resource(), service_id=service, config_version=1)
        assert await client.cleanup() == [] and pending == []
        assert calls == ['register', 'release']
        await client.close()
    asyncio.run(run())


def test_rejected_managed_vision_job_is_recorded_and_cleaned_without_resubmission():
    import pytest
    async def run():
        service, pending, calls = uuid4(), [], []
        async def load(): return list(pending)
        async def save(rows): pending[:] = rows
        class Resource:
            async def state(self): return {'boot_id': 'boot-1'}
            async def set_pin(self, *_): pass
            async def register(self, *_): return {'state': 'registered'}
            async def submit(self, *_): calls.append('submit'); return 'rejected'
            async def reject(self, *_):
                assert pending[0]['registration_state'] == 'rejected'
                calls.append('reject'); return True
            async def release(self, *_): calls.append('release'); return 'retained'
        def handler(request):
            if request.url.path == '/system_stats':
                return httpx.Response(200, json={'system': {'comfyui_version': '0.43.0'}})
            raise AssertionError(request.url.path)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='http://localhost:8188')
        client = ComfyWorkflowClient('http://localhost:8188', 'owner', load, save, client=http,
                                     resource=Resource(), service_id=service, config_version=1)
        with pytest.raises(ValueError, match='comfy_workflow_failed'):
            await client.generate('描述图片')
        assert calls == ['submit', 'reject', 'release'] and pending == []
        await client.close()
    asyncio.run(run())
