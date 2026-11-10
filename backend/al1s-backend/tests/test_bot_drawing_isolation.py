import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from al1s.adapters.postgres.bot_drawing_repository import BotDrawingRepository
from al1s.meme_workshop.qq_worker import QqWorker


def message(text):
    return {
        "post_type": "message", "message_type": "group", "self_id": 100,
        "user_id": 200, "group_id": 300, "message_id": 123,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def test_ordinary_messages_never_enter_drawing():
    async def run():
        calls = []

        class Chat:
            reserved = {}

            async def accept(self, *_):
                calls.append("chat")

        class Drawing:
            async def accept(self, *_):
                calls.append("drawing")
                return False

        worker = QqWorker(SimpleNamespace(), object(), ai_adapter=Chat(), drawing_adapter=Drawing())
        await worker.handle_event(SimpleNamespace(service_id=uuid4()), object(), message("你好，武尊"))
        assert calls == ["chat"]

    asyncio.run(run())


def test_drawing_authorization_does_not_use_chat_settings():
    service_id = uuid4()

    class Session:
        def get(self, model, key, **_kwargs):
            if model.__name__ == "BotServiceRow":
                return SimpleNamespace(enabled=True, deleted_at=None, kind="onebot_gateway")
            if model.__name__ == "BotDrawingSettingsRow":
                return SimpleNamespace(enabled=True, applied_version=7, row_version=7, allow_private=True)
            if model.__name__ == "BotDrawingScopeRow":
                return object() if key == (service_id, "300") else None
            if model.__name__ == "BotAiSettingsRow":
                return SimpleNamespace(enabled=False, desired_version=9, applied_version=7, allow_private=False)
            return None

    group = SimpleNamespace(scope_kind="qq_group", target_id="300", sender_id="200")
    private = SimpleNamespace(scope_kind="qq_private", target_id="200", sender_id="200")
    denied = SimpleNamespace(scope_kind="qq_group", target_id="999", sender_id="200")
    BotDrawingRepository._allowed(Session(), service_id, group)
    BotDrawingRepository._allowed(Session(), service_id, private)
    with pytest.raises(ValueError, match="drawing_scope_denied"):
        BotDrawingRepository._allowed(Session(), service_id, denied)


def test_drawing_command_works_without_chat_and_never_falls_through():
    async def run():
        calls = []

        class Drawing:
            async def accept(self, *_):
                calls.append("drawing")
                return False

        worker = QqWorker(SimpleNamespace(), object(), drawing_adapter=Drawing())
        await worker.handle_event(SimpleNamespace(service_id=uuid4()), object(), message("/wf3 画一只猫"))
        assert calls == ["drawing"]

        class Chat:
            reserved = {}

            async def accept(self, *_):
                calls.append("chat")

        worker._ai_adapter = Chat()
        await worker.handle_event(SimpleNamespace(service_id=uuid4()), object(), message("/wf3 画一只猫"))
        assert calls == ["drawing", "drawing"]
        worker._drawing_adapter = None
        await worker.handle_event(SimpleNamespace(service_id=uuid4()), object(), message("/wf3 画一只猫"))
        assert calls == ["drawing", "drawing"]

    asyncio.run(run())


def test_event_address_conflict_is_reported_instead_of_silently_using_another_connection():
    service = uuid4()
    worker = QqWorker(SimpleNamespace(), object())
    worker._tasks[service] = (SimpleNamespace(event_url="ws://chat:3001"), object())
    assert worker.drawing_connection_status(service, "ws://drawing:3001") == "conflict"
    assert worker.drawing_connection_status(service, "ws://chat:3001") == "connecting"
    worker._sockets[service] = object()
    assert worker.drawing_connection_status(service, "ws://chat:3001") == "connected"


def test_worker_opens_drawing_connection_without_ai_chat():
    async def run():
        service, calls = uuid4(), []
        row = SimpleNamespace(service_id=service, event_url='ws://bot:3001', base_url='http://bot:3000')

        class Repo:
            def list_templates(self, **_kwargs): return []

        class Drawing:
            async def refresh(self): return [row]

        async def connections(): return []
        worker = QqWorker(SimpleNamespace(repository=Repo()), object(), connections=connections,
                          drawing_adapter=Drawing())

        async def listen(connection): calls.append(connection.service_id)
        worker.listen = listen
        await worker.refresh()
        await asyncio.sleep(0)
        assert calls == [service] and service in worker._tasks

    asyncio.run(run())


def test_drawing_capability_does_not_require_ai_chat_capability():
    from al1s.meme_workshop.runtime_identity import RuntimeIdentity
    from al1s.components.host.bot_proof import RuntimeProof

    identity = RuntimeIdentity(role='qq', runtime_version='1.0.0', source_sha256='a' * 64,
                               requirements_sha256='b' * 64, qq_comfy_drawing_v1=True)
    proof = RuntimeProof(role='qq', native_version='test', runtime_version='test',
                         render_proof=True, qq_comfy_drawing_v1=True)
    assert identity.qq_comfy_drawing_v1 and not identity.qq_ai_v1
    assert proof.qq_comfy_drawing_v1 and not proof.qq_ai_v1


def test_green_explicit_prompt_does_not_request_chat_model():
    from al1s.bot_drawing.commands import parse_drawing_event
    from tests.test_bot_drawing_qq import Client, event, fixture

    async def run():
        service, row, calls = fixture()

        command = parse_drawing_event(event("/wf3 正面：blue cat 负面：blur", at=False), "100")
        assert await service._drawing(row, Client(), command, {"applied_version": 1}, None)
        assert ("admit", "200", "blue cat") in calls
        assert not any(item[0] == "expand" for item in calls)

    asyncio.run(run())


def test_wish_uses_only_drawing_provider_and_closes_client():
    from al1s.bot_drawing.contracts import PromptIntent
    from al1s.bot_drawing.prompt_model import DrawingPromptModel

    async def run():
        service_id, profile_id, calls = uuid4(), uuid4(), []

        class Repo:
            def prompt_credentials(self, requested, profile, version):
                assert (requested, profile, version) == (service_id, profile_id, 7)
                return SimpleNamespace(adapter_kind="openai_chat_completion", model_id="draw-model"), "draw-key"

        class Client:
            async def close(self):
                calls.append("closed")

        def client_factory(config, secret):
            calls.append((config.model_id, secret))
            return Client()

        class Provider:
            def __init__(self, kind, model, client):
                calls.append((kind, model))

            async def generate(self, *_args, **_kwargs):
                return {"text": '{"positive":"blue cat","negative":"blur"}', "tool_calls": []}

        model = DrawingPromptModel(Repo(), client_factory=client_factory, provider_factory=Provider)
        result = await model.expand_drawing(service_id, profile_id, 7, "画一只蓝猫", PromptIntent(mode="wish"),
                                            workflow_name="MOSS", aliases=[])
        assert result == ("blue cat", "blur")
        assert calls == [("draw-model", "draw-key"), ("openai_chat_completion", "draw-model"), "closed"]

    asyncio.run(run())


def test_wish_is_queued_without_using_chat_or_expanding_before_its_turn():
    from tests.test_bot_drawing_qq import Client, event, fixture

    async def run():
        service, row, calls = fixture()

        assert await service.accept(row, Client(), event('/wf3 画一只猫'))
        assert ('wish', '画一只猫') in calls
        assert not any(item[0] == 'expand' for item in calls)

    asyncio.run(run())
