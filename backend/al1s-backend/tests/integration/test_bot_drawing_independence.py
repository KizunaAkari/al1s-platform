from uuid import UUID, uuid4

import pytest

from al1s.adapters.postgres.bot_ai_models import BotAiScopeRow, BotAiSettingsRow
from al1s.adapters.postgres.bot_ai_repository import BotAiRepository
from al1s.adapters.postgres.bot_drawing_models import (
    BotDrawingGroupModeRow, BotDrawingPromptProfileRow, BotDrawingPromptRevisionRow, BotDrawingScopeRow,
    BotDrawingSelectionRow, BotDrawingSettingsRow, BotDrawingTaskRow,
)
from al1s.api.bot_drawing import PromptProviderWrite
from al1s.adapters.postgres.bot_drawing_repository import BotDrawingRepository
from al1s.adapters.postgres.bot_models import BotConfigVersionRow, BotServiceRow
from al1s.adapters.postgres.notification_models import EncryptedSecretRow
from al1s.bot_ai.settings import AiSettingsWrite
from al1s.bot_drawing.contracts import PromptIntent
from al1s.meme_workshop.contracts import CommandSource
from al1s.secrets.security import FernetSecretCipher
from tests.integration.test_bot_drawing_persistence import add_preset, add_service, sessions

pytestmark = pytest.mark.integration


def setup(sessions, name="independent-drawing"):
    with sessions.begin() as session:
        service = add_service(session, name)
        session.add(BotAiSettingsRow(service_id=service, enabled=False, allow_private=False,
                                    desired_version=3, applied_version=2))
        session.add(BotDrawingSettingsRow(service_id=service, enabled=True, allow_private=True,
            event_ws_url="ws://example.invalid:3001", row_version=1, applied_version=1, resource_applied_version=1))
        session.flush()
        session.add(BotAiScopeRow(service_id=service, scope_kind="qq_group", target_id="999"))
        session.add(BotDrawingScopeRow(service_id=service, group_id="123"))
        preset = add_preset(session, service)
        session.get(BotDrawingSettingsRow, service).default_text_preset_id = preset
        session.add(BotDrawingGroupModeRow(service_id=service, group_id="123", mode="r18"))
        session.add(BotDrawingSelectionRow(service_id=service, scope_kind="qq_group", target_id="123",
                                          sender_id="456", preset_id=preset))
    return service, preset


def source(group="123"):
    return CommandSource(scope_kind="qq_group", target_id=group, sender_id="456", message_id="77")


def test_drawing_scope_private_and_route_ignore_chat_disabled_or_unsynced(sessions):
    service, preset = setup(sessions)
    repo = BotDrawingRepository(sessions)
    route = repo.prepare_route(service, source(), PromptIntent(mode="direct", positive="cat"), "txt2img")
    assert route.decision.workflow.id == preset
    assert route.ai_version is None and route.chat_profile_id is None
    assert repo.allowed(service, source(), 1)
    assert not repo.allowed(service, source("999"), 1)
    private = CommandSource(scope_kind="qq_private", target_id="456", sender_id="456", message_id="78")
    assert repo.allowed(service, private, 1)


def test_drawing_establishes_its_own_qq_connection_when_chat_is_disabled(sessions):
    service, _ = setup(sessions)
    with sessions.begin() as session:
        config = BotConfigVersionRow(id=uuid4(), service_id=service, version_no=1,
            settings={'ONEBOT_BASE_URL': 'http://example.invalid:3000'}, config_hash='a' * 64)
        session.add(config)
        session.flush()
        session.get(BotServiceRow, service).applied_config_version_id = config.id
    repo = BotDrawingRepository(sessions)
    rows = repo.qq_connections()
    assert len(rows) == 1 and rows[0].service_id == service
    assert rows[0].event_url == 'ws://example.invalid:3001'
    with sessions.begin() as session:
        session.get(BotDrawingSettingsRow, service).enabled = False
    assert repo.qq_connections() == []


def test_chat_scope_changes_do_not_delete_drawing_authorization_or_preferences(sessions):
    service, _ = setup(sessions)
    cipher = FernetSecretCipher("drawing-test-key-with-at-least-32-characters")
    ai = BotAiRepository(sessions, cipher)
    ai.save_settings(service, AiSettingsWrite(groups=[]), expected_version=3, operation_id="remove-chat-group")
    with sessions() as session:
        assert session.get(BotDrawingScopeRow, (service, "123")) is not None
        assert session.get(BotDrawingGroupModeRow, (service, "123")).mode == "r18"
        assert session.get(BotDrawingSelectionRow, (service, "qq_group", "123", "456")) is not None
    assert BotDrawingRepository(sessions).allowed(service, source(), 1)


def test_drawing_scope_updates_are_versioned_and_preserve_chat_and_group_preferences(sessions):
    service, preset = setup(sessions)
    repo = BotDrawingRepository(sessions)
    args = dict(enabled=True, keep_drawing_warm=False, default_text_preset_id=preset,
        default_edit_preset_id=None, allow_private=False, event_ws_url="ws://example.invalid:3001",
        comfy_base_url="http://host.docker.internal:8188", groups=["321"], expected_version=1, operation_id="scope")
    saved = repo.save_settings(service, **args)
    assert saved.row_version == 2 and saved.groups == ("321",)
    assert repo.save_settings(service, **args) == saved
    with sessions() as session:
        assert session.get(BotAiScopeRow, (service, "qq_group", "999")) is not None
        assert session.get(BotDrawingGroupModeRow, (service, "123")).mode == "r18"
    assert repo.mark_applied(service, 2)
    assert not repo.allowed(service, source(), 2)
    assert repo.allowed(service, source("321"), 2)
    with pytest.raises(ValueError, match="drawing_settings_conflict"):
        repo.save_settings(service, **{**args, "operation_id": "stale"})


def test_prompt_model_pool_keeps_current_snapshot_and_uses_new_model_for_next_task(sessions):
    service, _ = setup(sessions)
    cipher = FernetSecretCipher("drawing-test-key-with-at-least-32-characters")
    repo = BotDrawingRepository(sessions, cipher=cipher)
    model_a = PromptProviderWrite(name="模型 A", adapter_kind="openai_chat_completion", model_id="model-a",
                                  secret_input="synthetic-key-a", network_mode="direct")
    model_b = PromptProviderWrite(name="模型 B", adapter_kind="openai_chat_completion", model_id="model-b",
                                  secret_input="synthetic-key-b", network_mode="direct")
    first = repo.save_prompt_model(service, model_a, profile_id=None, expected_version=0, operation_id="create-a")
    second = repo.save_prompt_model(service, model_b, profile_id=None, expected_version=0, operation_id="create-b")
    assert repo.save_prompt_model(service, model_a, profile_id=None, expected_version=0, operation_id="create-a") == first
    assert "synthetic-key-a" not in str(first)
    a_id, b_id = UUID(first["id"]), UUID(second["id"])
    repo.activate_prompt_model(service, a_id, expected_version=1, operation_id="activate-a")
    tasks = []
    for message in ("1001", "1002"):
        request = source().model_copy(update={"message_id": message})
        prepared = repo.prepare_route(service, request, PromptIntent(mode="wish"), "txt2img")
        tasks.append(repo.admit(service, request, "888", "test", prepared, positive="", negative="",
                                wish_text="画一只猫", seed=1))
    frozen = repo.begin_prompt_expansion(tasks[0])
    assert frozen == (a_id, 1)
    updated = repo.save_prompt_model(service,
        PromptProviderWrite(name="模型 A 新名称", adapter_kind="openai_chat_completion", model_id="model-a-new",
                            secret_input="synthetic-key-new", network_mode="direct"),
        profile_id=a_id, expected_version=first["row_version"], operation_id="edit-a")
    repo.activate_prompt_model(service, b_id, expected_version=2, operation_id="activate-b")
    repo.delete_prompt_model(service, a_id, expected_version=updated["row_version"], operation_id="delete-a")
    config, secret = repo.prompt_credentials(service, *frozen)
    assert config.model_id == "model-a" and secret == "synthetic-key-a"
    assert repo.begin_prompt_expansion(tasks[1]) == (b_id, 1)
    repo.finish_prompt_expansion(tasks[0], *frozen, "cat", "")
    with sessions() as session:
        row = session.get(BotDrawingTaskRow, tasks[0])
        revision = session.get(BotDrawingPromptRevisionRow, frozen)
        assert row.prompt_status == "ready" and row.positive == "cat"
        assert session.get(EncryptedSecretRow, revision.secret_id).ciphertext != "synthetic-key-a"
        assert session.get(BotDrawingPromptProfileRow, a_id).deleted_at is not None
        assert session.get(BotDrawingSettingsRow, service).row_version == 1


def test_prompt_model_selection_and_ownership_use_independent_versions(sessions):
    service, _ = setup(sessions)
    other, _ = setup(sessions, "other-drawing")
    repo = BotDrawingRepository(sessions, cipher=FernetSecretCipher("drawing-test-key-with-at-least-32-characters"))
    model = PromptProviderWrite(name="模型", adapter_kind="openai_chat_completion", model_id="draw-model",
                                secret_input="synthetic-key")
    saved = repo.save_prompt_model(service, model, profile_id=None, expected_version=0, operation_id="create")
    identity = UUID(saved["id"])
    with pytest.raises(ValueError, match="drawing_prompt_not_found"):
        repo.activate_prompt_model(other, identity, expected_version=1, operation_id="foreign")
    repo.activate_prompt_model(service, identity, expected_version=1, operation_id="activate")
    with pytest.raises(ValueError, match="drawing_prompt_conflict"):
        repo.activate_prompt_model(service, identity, expected_version=1, operation_id="stale")
    with pytest.raises(ValueError, match="drawing_prompt_name_duplicate"):
        repo.save_prompt_model(service, model, profile_id=None, expected_version=0, operation_id="duplicate")
    with sessions() as session:
        secret_id = session.get(BotDrawingPromptRevisionRow, (identity, 1)).secret_id
    deleted = repo.delete_prompt_model(service, identity, expected_version=1, operation_id="delete")
    assert deleted["deleted"] and deleted["active_id"] is None and deleted["selection_version"] == 3
    assert repo.delete_prompt_model(service, identity, expected_version=1, operation_id="delete") == deleted
    with sessions() as session:
        assert session.get(BotDrawingPromptProfileRow, identity) is None
        assert session.get(EncryptedSecretRow, secret_id) is None


def test_queued_wish_freezes_model_only_when_it_reaches_the_front(sessions):
    import asyncio
    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_drawing.runner import DrawingTaskRunner

    service, _ = setup(sessions)
    repo = BotDrawingRepository(sessions, cipher=FernetSecretCipher("drawing-test-key-with-at-least-32-characters"))
    a = repo.save_prompt_model(service,
        PromptProviderWrite(name="A", adapter_kind="openai_chat_completion", model_id="model-a", secret_input="key-a"),
        profile_id=None, expected_version=0, operation_id="a")
    b = repo.save_prompt_model(service,
        PromptProviderWrite(name="B", adapter_kind="openai_chat_completion", model_id="model-b", secret_input="key-b"),
        profile_id=None, expected_version=0, operation_id="b")
    a_id, b_id = UUID(a["id"]), UUID(b["id"])
    repo.activate_prompt_model(service, a_id, expected_version=1, operation_id="select-a")
    tasks = []
    for message in ("4001", "4002"):
        request = source().model_copy(update={"message_id": message})
        prepared = repo.prepare_route(service, request, PromptIntent(mode="wish"), "txt2img")
        tasks.append(repo.admit(service, request, "888", "test", prepared, positive="", negative="",
                                wish_text="画一只猫", seed=1))

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        class PromptModel:
            async def expand_drawing(self, _service, profile, revision, *_args, **_kwargs):
                calls.append((profile, revision))
                if profile == a_id:
                    started.set()
                    await release.wait()
                return "cat", ""
        queue = GpuQueue()
        for task in tasks:
            queue.reserve(str(task))
        runner = DrawingTaskRunner(repo, None, None, None, None, lambda _: None,
                                   gpu_queue=queue, prompt_model=PromptModel())
        assert await runner.register(tasks[1]) == "waiting_turn"
        assert (await asyncio.to_thread(repo.task_for_worker, tasks[1]))["prompt_profile_id"] is None
        assert await runner.register(tasks[0]) == "prompting"
        first = runner._prompt_tasks[tasks[0]]
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.to_thread(repo.activate_prompt_model, service, b_id, expected_version=2, operation_id="select-b")
        release.set()
        await asyncio.wait_for(first, timeout=2)
        queue.finish(str(tasks[0]))
        assert await runner.register(tasks[1]) == "prompting"
        await asyncio.wait_for(runner._prompt_tasks[tasks[1]], timeout=2)
        assert calls == [(a_id, 1), (b_id, 1)]
        assert (await asyncio.to_thread(repo.task_for_worker, tasks[0]))["prompt_status"] == "ready"
        assert (await asyncio.to_thread(repo.task_for_worker, tasks[1]))["prompt_status"] == "ready"
    asyncio.run(run())


def test_force_cancel_during_prompt_expansion_allows_next_queued_task(sessions):
    import asyncio
    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_drawing.runner import DrawingTaskRunner

    service, _ = setup(sessions)
    repo = BotDrawingRepository(sessions, cipher=FernetSecretCipher("drawing-test-key-with-at-least-32-characters"))
    model = repo.save_prompt_model(service,
        PromptProviderWrite(name="A", adapter_kind="openai_chat_completion", model_id="model-a", secret_input="key-a"),
        profile_id=None, expected_version=0, operation_id="create")
    repo.activate_prompt_model(service, UUID(model["id"]), expected_version=1, operation_id="select")
    tasks = []
    for message in ("5001", "5002"):
        request = source().model_copy(update={"message_id": message})
        prepared = repo.prepare_route(service, request, PromptIntent(mode="wish"), "txt2img")
        tasks.append(repo.admit(service, request, "888", "test", prepared, positive="", negative="",
                                wish_text="画一只猫", seed=1))

    async def run():
        started = asyncio.Event()
        calls = []
        class PromptModel:
            async def expand_drawing(self, *_args, **_kwargs):
                calls.append("started")
                if len(calls) == 1:
                    started.set()
                    await asyncio.Event().wait()
                return "cat", ""
        queue = GpuQueue()
        for task in tasks:
            queue.reserve(str(task))
        runner = DrawingTaskRunner(repo, None, None, None, None, lambda _: None,
                                   gpu_queue=queue, prompt_model=PromptModel())
        assert await runner.register(tasks[0]) == "prompting"
        first = runner._prompt_tasks[tasks[0]]
        await asyncio.wait_for(started.wait(), timeout=2)
        assert await asyncio.to_thread(repo.request_cancel, service, tasks[0], operation_id="cancel") == "cancelled"
        await runner.cancelled(tasks[0])
        await asyncio.gather(first, return_exceptions=True)
        assert queue.active == str(tasks[1])
        assert await runner.register(tasks[1]) == "prompting"
        await asyncio.wait_for(runner._prompt_tasks[tasks[1]], timeout=2)
        assert (await asyncio.to_thread(repo.task_for_worker, tasks[0]))["generation_status"] == "cancelled"
        assert (await asyncio.to_thread(repo.task_for_worker, tasks[1]))["prompt_status"] == "ready"
    asyncio.run(run())
