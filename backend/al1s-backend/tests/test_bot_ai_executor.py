import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from al1s.bot_ai.contracts import AiEnvelope, AiInputPart
from al1s.bot_ai.executor import AiExecutor
from al1s.meme_workshop.contracts import CommandSource


class History:
    def __init__(self): self.saved, self.cleared = [], []
    def context(self, *_args): return SimpleNamespace(messages=[{'role': 'user', 'content': '旧消息'}])
    def append(self, context, request, turn, **guard): self.saved.append((context, request, turn, guard))
    def clear(self, *args): self.cleared.append(args)


class Invocation:
    def __init__(self): self.epoch, self.parts, self.guards = 'epoch1', [], []
    async def blocking(self, function, *args, **kwargs): return function(*args, **kwargs)
    async def output(self, sequence, parts):
        self.parts.append((sequence, parts))
        return {'status': 'sent'}
    async def tool(self, *_args): return {'status': 'success'}
    def guard_image(self, *args): self.guards.append(args)


class Provider:
    async def generate(self, *_args, **_kwargs):
        return {'text': '你好', 'tool_calls': []}


def envelope(profile, *, smart=None, tts=False):
    return AiEnvelope(request_id=uuid4(), service_id=uuid4(), config_version=2,
        source=CommandSource(message_id='1', scope_kind='qq_group', target_id='100', sender_id='200'),
        self_id='300', sender_name='小明', parts=[{'type':'text','text':'嗨'}],
        chat_profile_id=profile, persona_prompt='personality', smart_reply=smart, tts_enabled=tts,
        deadline=datetime.now(UTC)+timedelta(seconds=30))


def test_executor_persists_guarded_turn_and_sends_reply(tmp_path):
    async def run():
        identity = uuid4()
        item = envelope(identity)
        history, invocation = History(), Invocation()
        engine = AiExecutor({identity: SimpleNamespace(provider=Provider(), supports_vision=False)},
                            history, tmp_path)
        assert await engine.run(item, invocation) == ('completed', None)
        assert invocation.parts == [(0, [{'type':'text','text':'你好'}])]
        assert history.saved[0][1] == item.request_id
        assert history.saved[0][3] == {'epoch':'epoch1', 'config_version':2}
        assert history.saved[0][2][0]['role'] == 'user' and history.saved[0][2][-1]['role'] == 'assistant'
    asyncio.run(run())


def test_shared_text_slots_limit_model_calls_after_media_preparation(tmp_path):
    async def run():
        identity, slots, first_started, release = uuid4(), asyncio.Semaphore(1), asyncio.Event(), asyncio.Event()
        calls = []
        class SlowProvider:
            async def generate(self, *_args, **_kwargs):
                calls.append("start")
                first_started.set()
                await release.wait()
                return {"text": "ok", "tool_calls": []}
        engine = AiExecutor({identity: SimpleNamespace(provider=SlowProvider(), supports_vision=False)},
                            History(), tmp_path, text_slots=slots)
        first = asyncio.create_task(engine.run(envelope(identity), Invocation()))
        await first_started.wait()
        second = asyncio.create_task(engine.run(envelope(identity), Invocation()))
        await asyncio.sleep(0.02)
        assert calls == ["start"]
        release.set()
        assert await first == ("completed", None)
        assert await second == ("completed", None)
        assert calls == ["start", "start"]
    asyncio.run(run())


def test_smart_silence_sends_nothing_and_does_not_add_history(tmp_path):
    async def run():
        identity = uuid4()
        item = envelope(identity, smart={})
        history, invocation = History(), Invocation()
        class Silent:
            async def decide(self, *_args, **_kwargs): return SimpleNamespace(should_reply=False, code='ai_silent')
        engine = AiExecutor({identity: SimpleNamespace(provider=Provider(), supports_vision=False)},
                            history, tmp_path, policy=Silent())
        assert await engine.run(item, invocation) == ('completed', 'ai_silent')
        assert not invocation.parts and not history.saved
    asyncio.run(run())


def test_smart_policy_keeps_cooldown_and_group_isolation():
    from al1s.bot_ai.managed.smart_contract import AiSmartOptions
    from al1s.bot_ai.managed.smart_reply import SmartReplyPolicy

    async def run():
        policy, options = SmartReplyPolicy(), AiSmartOptions()

        async def ask(**_kwargs):
            return SimpleNamespace(completion_text='{"should_reply":true,"channels":["text"]}')

        first = await policy.decide('group:1', '200', '你好', options, ask, {'text'}, now=100)
        assert first.should_reply and first.channels == ('text',)
        policy.sent('group:1', options, now=100)
        blocked = await policy.decide('group:1', '200', '再见', options, ask, {'text'}, now=101)
        other = await policy.decide('group:2', '200', '你好', options, ask, {'text'}, now=101)
        assert blocked.code == 'ai_rate_limited' and other.should_reply

    asyncio.run(run())


def test_smart_reply_can_compose_text_voice_and_sticker(tmp_path):
    async def run():
        identity = uuid4()
        item = envelope(identity, smart={'voice_enabled':True}, tts=True)
        item = item.model_copy(update={'sticker_enabled':True})
        history, invocation = History(), Invocation()
        class Policy:
            async def decide(self, *_args, **_kwargs):
                return SimpleNamespace(should_reply=True, code='ai_reply_selected',
                                       channels=('text', 'voice', 'sticker'), sticker_index=1)
            def sent(self, *_args, **_kwargs): pass
        class Library:
            async def search(self, *_args, **_kwargs): return [{'id':'a'*64,'index':1,'description':'开心'}]
            async def stage(self, *_args): return '/request/sticker.gif'
            def allowed_now(self, *_args): return True
        async def tts(_text, _request): return '/request/voice.wav'
        engine = AiExecutor({identity: SimpleNamespace(provider=Provider(), supports_vision=False)},
                            history, tmp_path, policy=Policy(), stickers=Library(), tts=tts)
        assert await engine.run(item, invocation) == ('completed', None)
        assert [part['type'] for part in invocation.parts[0][1]] == ['text','record','image']
        assert invocation.guards and invocation.guards[0][2] == '你好'
    asyncio.run(run())


def test_missing_configured_vision_cannot_silently_send_an_unread_image(tmp_path):
    async def run():
        identity = uuid4()
        item = envelope(identity).model_copy(update={'vision_provider_id': 'fixture_vision',
            'parts': (AiInputPart(type='image', media_ref='/unused'),)})
        history, invocation = History(), Invocation()
        engine = AiExecutor({identity: SimpleNamespace(provider=Provider(), supports_vision=False)},
                            history, tmp_path)
        with pytest.raises(ValueError, match='vision'):
            await engine.run(item, invocation)
        assert not history.saved and not invocation.parts
    asyncio.run(run())


def test_clear_resets_only_the_target_history_and_smart_context(tmp_path):
    async def run():
        service, history = uuid4(), History()
        class Policy:
            forgotten = []
            def forget_origin(self, value): self.forgotten.append(value)
        policy = Policy()
        engine = AiExecutor({}, history, tmp_path, policy=policy)
        assert await engine.clear_session(service, 'qq_group', '100') == {'state':'cleared'}
        assert history.cleared == [(service,'qq_group','100')]
        assert policy.forgotten == [f'{service}:qq_group:100']
    asyncio.run(run())


def test_clear_cancellation_waits_for_storage_before_releasing_session(tmp_path):
    async def run():
        import threading
        service, started, release = uuid4(), threading.Event(), threading.Event()
        class SlowHistory:
            def clear(self, *_args):
                started.set()
                assert release.wait(3)
        class Policy:
            forgotten = []
            def forget_origin(self, value): self.forgotten.append(value)
        policy = Policy()
        engine = AiExecutor({}, SlowHistory(), tmp_path, policy=policy)
        task = asyncio.create_task(engine.clear_session(service, 'qq_group', '100'))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert policy.forgotten == [f'{service}:qq_group:100']
    asyncio.run(run())
