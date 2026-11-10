import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

from al1s.meme_workshop.qq_worker import QqWorker


def test_ordinary_event_only_reaches_chat():
    async def run():
        calls = []
        class Ai:
            reserved = {}
            async def accept(self, *_): calls.append('chat')
        class Drawing:
            handled = True
            async def accept(self, *_): calls.append('drawing'); return self.handled
        drawing, ai = Drawing(), Ai()
        worker = QqWorker(SimpleNamespace(), object(), ai_adapter=ai, drawing_adapter=drawing)
        row = SimpleNamespace(service_id=uuid4())
        event = {'post_type': 'message', 'message_type': 'group', 'self_id': 100, 'user_id': 200,
                 'group_id': 300, 'message_id': 123, 'time': int(time.time()),
                 'message': [{'type': 'at', 'data': {'qq': '100'}}, {'type': 'text', 'data': {'text': '你好'}}]}
        await worker.handle_event(row, object(), event)
        assert calls == ['chat']
        calls.clear(); drawing.handled = False
        await worker.handle_event(row, object(), event)
        assert calls == ['chat']
    asyncio.run(run())


def test_ordinary_chat_reaches_ai_without_drawing_classification():
    from tests.test_bot_drawing_qq import Client, event, fixture

    async def run():
        drawing, row, calls = fixture(mode='chat')
        class Ai:
            reserved = {}
            def __init__(self): self.received = []
            async def accept(self, _row, _client, message): self.received.append(message)
        ai = Ai()
        worker = QqWorker(SimpleNamespace(), object(), ai_adapter=ai, drawing_adapter=drawing)
        message = event('你好，武尊')
        await worker.handle_event(row, Client(), message)
        assert ai.received == [message]
        assert calls == []
    asyncio.run(run())


def test_explicit_workflow_is_handled_before_ordinary_chat():
    from tests.test_bot_drawing_qq import Client, event, fixture

    async def run():
        drawing, row, calls = fixture()
        class Ai:
            reserved = {}
            def __init__(self): self.received = []
            async def accept(self, _row, _client, message): self.received.append(message)
        ai = Ai()
        worker = QqWorker(SimpleNamespace(), object(), ai_adapter=ai, drawing_adapter=drawing)
        await worker.handle_event(row, Client(), event('/wf3 正面：blue cat', at=False))
        assert ai.received == []
        assert ('workflow_number', 3) in calls and ('admit', '200', 'blue cat') in calls
    asyncio.run(run())


def test_online_gpt_command_reaches_drawing_without_entering_chat_or_gpu_queue():
    from tests.test_bot_drawing_qq import Client, event, fixture

    async def run():
        drawing, row, calls = fixture()
        class Ai:
            reserved = {}
            async def accept(self, *_): calls.append(('chat',))
        def admit_online(service, **kwargs):
            calls.append(('online', kwargs['preset_number'], kwargs['positive']))
            return kwargs['task_id']
        drawing.repository.admit_online = admit_online
        worker = QqWorker(SimpleNamespace(), object(), ai_adapter=Ai(), drawing_adapter=drawing)
        await worker.handle_event(row, Client(), event('/gpt7 画一只猫', at=False))
        assert ('online', 7, '画一只猫') in calls and ('chat',) not in calls
        assert not any(item[0] in {'route', 'admit', 'expand'} for item in calls)
    asyncio.run(run())


def test_online_gpt_splits_marked_negative_and_preserves_original_wish():
    from tests.test_bot_drawing_qq import Client, event, fixture

    async def run():
        drawing, row, calls = fixture()
        def admit_online(service, **kwargs):
            calls.append(('conditions', kwargs['positive'], kwargs['negative'], kwargs.get('original_positive')))
            return kwargs['task_id']
        drawing.repository.admit_online = admit_online
        assert await drawing.accept(row, Client(), event('/gpt7 1girl【NP】blurry', at=False))
        assert ('conditions', '1girl', 'blurry', '1girl【NP】blurry') in calls
    asyncio.run(run())
