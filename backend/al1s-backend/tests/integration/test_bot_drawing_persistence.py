import os
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.bot_models import BotServiceRow
from al1s.adapters.postgres import notification_models  # Registers the secret FK target.
from al1s.adapters.postgres.bot_ai_models import BotAiChatProfileRow, BotAiRequestRow, BotAiScopeRow, BotAiSessionProfileRow, BotAiSettingsRow
from al1s.adapters.postgres.bot_ai_repository import BotAiRepository
from al1s.adapters.postgres.bot_ai_history import AiHistoryRepository
from al1s.adapters.postgres.notification_models import EncryptedSecretRow
from al1s.adapters.postgres.secret_repository import PostgresSecretRepository
from al1s.adapters.postgres.bot_drawing_repository import BotDrawingRepository, _dependency_signature
from al1s.adapters.postgres.bot_drawing_models import (
    BotDrawingGroupModeRow, BotDrawingGroupPresetRow, BotDrawingPresetRow, BotDrawingRevisionRow,
    BotDrawingScopeRow, BotDrawingSelectionRow, BotDrawingSettingsRow, BotDrawingTaskRow,
)
from al1s.bot_ai.settings import AiSettingsWrite
from al1s.bot_drawing.contracts import AssetDependency, InputBindings, OutputBinding, PortBinding, PromptIntent, ValidatedWorkflow
from al1s.bot_drawing.workflow import MAX_GRAPH_BYTES, _hash, _snapshot
from al1s.meme_workshop.contracts import CommandSource
from al1s.secrets.security import FernetSecretCipher

pytestmark = pytest.mark.integration


@pytest.fixture
def sessions():
    url = os.environ.get("AL1S_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated PostgreSQL required")
    database = make_url(url).database or ""
    if not database.startswith("al1s_comfy_test_"):
        raise RuntimeError("drawing tests require a dedicated al1s_comfy_test_ database")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE bot_services CASCADE"))
    yield sessionmaker(engine, expire_on_commit=False)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE bot_services CASCADE"))
    engine.dispose()


def add_service(session, name):
    service = BotServiceRow(id=uuid4(), name=name, kind="onebot_gateway", enabled=True)
    session.add(service)
    session.flush()
    return service.id


def add_preset(session, service_id, number=1, kind="txt2img"):
    preset = BotDrawingPresetRow(id=uuid4(), service_id=service_id, number=number,
        name=f"workflow {number}", normalized_name=f"workflow {number}", kind=kind,
        enabled=True, current_revision=1, row_version=1)
    session.add(preset)
    session.flush()
    dependencies = {"adapter": "test", "boot_id": "boot", "nodes": [], "assets": []}
    session.add(BotDrawingRevisionRow(preset_id=preset.id, revision=1, service_id=service_id,
        source_graph={}, execution_template={}, source_bindings={}, bindings={}, manifest=[], graph_hash="a" * 64,
        execution_template_hash="b" * 64, snapshot_hash="c" * 64, adapter_version="test", validation_state="ready",
        dependency_manifest=dependencies, dependency_signature=_dependency_signature(dependencies)))
    session.flush()
    return preset.id


def make_task(service_id, preset_id, sender, source):
    return BotDrawingTaskRow(id=uuid4(), service_id=service_id, operation="draw", origin="qq",
        scope_kind="qq_group", target_id="123", sender_id=sender, bot_self_id="456",
        source_message_id=source, preset_id=preset_id, preset_revision=1,
        dependency_signature="a" * 64,
        endpoint_key="test", prompt_id=uuid4(), created_at=datetime.now(UTC))


def test_gpu_recovery_keeps_active_and_queued_tasks_in_created_order(sessions):
    with sessions.begin() as session:
        service = add_service(session, "recovery")
        preset = add_preset(session, service)
        first = make_task(service, preset, "1", "101")
        second = make_task(service, preset, "2", "102")
        first.generation_status = "running"
        second.created_at = first.created_at + timedelta(seconds=1)
        session.add_all([first, second])
        first_id, second_id = first.id, second.id
    rows = BotDrawingRepository(sessions).gpu_recovery("test")
    assert [(row["id"], row["active"]) for row in rows] == [(first_id, True), (second_id, False)]
    assert [row["owner"] for row in rows] == [(service, "1"), (service, "2")]


def test_gpu_recovery_uses_one_bounded_query_with_a_thousand_finished_tasks(sessions):
    with sessions.begin() as session:
        service = add_service(session, "bulk-recovery")
        preset = add_preset(session, service)
        rows = [make_task(service, preset, str(index + 1), str(index + 1)) for index in range(1000)]
        for row in rows:
            row.generation_status, row.cleanup_status = "succeeded", "done"
        waiting = make_task(service, preset, "5001", "5001")
        session.add_all([*rows, waiting])
        identity = waiting.id
    calls = []
    def record(_conn, _cursor, statement, *_args):
        if statement.lstrip().lower().startswith("select"):
            calls.append(statement)
    engine = sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        result = BotDrawingRepository(sessions).gpu_recovery("test")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(calls) == 1 and [row["id"] for row in result] == [identity]
    calls.clear()
    event.listen(engine, "before_cursor_execute", record)
    try:
        page = BotDrawingRepository(sessions).list_tasks(service, view="history", limit=20)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(page["items"]) == 20 and page["next_after_id"] is not None
    assert len(calls) == 2 and "LIMIT" in calls[-1].upper()


def test_drawing_defaults_on_for_new_connections_without_changing_explicitly_disabled_ones(sessions):
    with sessions.begin() as session:
        new = add_service(session, "new")
        disabled = add_service(session, "disabled")
        session.add(BotDrawingSettingsRow(service_id=disabled, enabled=False))
    cipher = FernetSecretCipher("drawing-test-key-with-at-least-32-characters")
    repo = BotDrawingRepository(sessions, cipher=cipher)
    assert repo.get_settings(new)["enabled"] is True
    repo.bind_resource_token(new, "resource-token-" + "a" * 40)
    repo.bind_resource_token(disabled, "resource-token-" + "b" * 40)
    assert repo.get_settings(new)["enabled"] is True
    assert repo.get_settings(disabled)["enabled"] is False


def test_ai_request_can_persist_a_gpu_queue_rejection(sessions):
    request_id = uuid4()
    with sessions.begin() as session:
        service = add_service(session, "ai-queue")
        now = datetime.now(UTC)
        session.add(BotAiRequestRow(id=request_id, service_id=service, scope_kind="qq_group", target_id="1",
            sender_id="2", source_message_id="3", config_version=1, status="running", send_status="none",
            created_at=now, expires_at=now + timedelta(minutes=30), sent_count=0))
    cipher = FernetSecretCipher("drawing-test-key-with-at-least-32-characters")
    assert BotAiRepository(sessions, cipher).confirm_finished(request_id, "failed", result_code="gpu_queue_full")
    with sessions() as session:
        assert session.get(BotAiRequestRow, request_id).result_code == "gpu_queue_full"


def test_old_infinite_retention_setting_cannot_be_silently_reenabled(sessions):
    with sessions.begin() as session:
        service = add_service(session, "legacy-retention")
    repo = BotDrawingRepository(sessions)
    with pytest.raises(ValueError, match="drawing_legacy_warm_unsupported"):
        repo.save_settings(service, enabled=True, keep_drawing_warm=True,
                           default_text_preset_id=None, default_edit_preset_id=None,
                           expected_version=0, operation_id="legacy")


def test_defaults_cannot_reference_another_connections_preset(sessions):
    with pytest.raises(IntegrityError):
        with sessions.begin() as session:
            first, second = add_service(session, "first"), add_service(session, "second")
            preset = add_preset(session, first)
            session.add(BotDrawingSettingsRow(service_id=second, default_text_preset_id=preset))
            session.flush()


def test_group_mode_and_personal_selection_are_isolated(sessions):
    with sessions.begin() as session:
        service = add_service(session, "one")
        first, second = add_preset(session, service, 1), add_preset(session, service, 2)
        session.add_all([
            BotDrawingGroupModeRow(service_id=service, group_id="123", mode="green"),
            BotDrawingSelectionRow(service_id=service, scope_kind="qq_group", target_id="123",
                sender_id="1", preset_id=first),
            BotDrawingSelectionRow(service_id=service, scope_kind="qq_group", target_id="123",
                sender_id="2", preset_id=second),
        ])
    with sessions() as session:
        values = dict(session.execute(select(BotDrawingSelectionRow.sender_id, BotDrawingSelectionRow.preset_id)
            .where(BotDrawingSelectionRow.service_id == service)).all())
        assert values == {"1": first, "2": second}
        assert session.get(BotDrawingGroupModeRow, (service, "123")).mode == "green"


def test_duplicate_source_is_rejected_but_same_user_can_queue_again(sessions):
    with sessions.begin() as session:
        service = add_service(session, "one")
        preset = add_preset(session, service)
        session.add(make_task(service, preset, "1", "100"))
    with pytest.raises(IntegrityError):
        with sessions.begin() as session:
            session.add(make_task(service, preset, "2", "100"))
    with sessions.begin() as session:
        session.add(make_task(service, preset, "1", "101"))
        session.add(make_task(service, preset, "2", "102"))


def test_qq_task_requires_a_concrete_scope(sessions):
    with sessions.begin() as session:
        service = add_service(session, "one")
        preset = add_preset(session, service)
    with pytest.raises(IntegrityError):
        with sessions.begin() as session:
            task = make_task(service, preset, "1", "100")
            task.scope_kind = None
            session.add(task)


def setup_scope(session):
    service = add_service(session, "one")
    session.add(BotAiSettingsRow(service_id=service, enabled=True, vision_enabled=True,
        desired_version=1, applied_version=1))
    session.flush()
    session.add(BotAiScopeRow(service_id=service, scope_kind="qq_group", target_id="123"))
    return service


def source(sender="1", group="123"):
    return CommandSource(message_id="100", scope_kind="qq_group", target_id=group, sender_id=sender)


def test_drawing_settings_cas_and_residency_do_not_change_vision(sessions):
    with sessions.begin() as session:
        service = setup_scope(session)
        preset = add_preset(session, service)
    repo = BotDrawingRepository(sessions)
    result = repo.save_settings(service, enabled=True, keep_drawing_warm=False,
        default_text_preset_id=preset, default_edit_preset_id=None, expected_version=0, operation_id="a")
    assert result.row_version == 1 and not result.keep_drawing_warm
    again = repo.save_settings(service, enabled=True, keep_drawing_warm=False,
        default_text_preset_id=preset, default_edit_preset_id=None, expected_version=0, operation_id="a")
    assert again == result
    changed = repo.save_settings(service, enabled=True, keep_drawing_warm=False,
        default_text_preset_id=preset, default_edit_preset_id=None, expected_version=1, operation_id="b")
    assert changed.row_version == 2 and not changed.keep_drawing_warm
    with pytest.raises(ValueError, match="drawing_settings_conflict"):
        repo.save_settings(service, enabled=False, keep_drawing_warm=False,
            default_text_preset_id=preset, default_edit_preset_id=None, expected_version=1, operation_id="c")
    with sessions() as session:
        assert session.get(BotAiSettingsRow, service).vision_enabled is True


def test_personal_workflow_selection_and_group_authorization(sessions):
    with sessions.begin() as session:
        service = setup_scope(session)
        one, two = add_preset(session, service, 1), add_preset(session, service, 2)
        session.add(BotDrawingSettingsRow(service_id=service, enabled=True, row_version=1, applied_version=1))
        session.flush()
        session.add(BotDrawingScopeRow(service_id=service, group_id='123'))
    repo = BotDrawingRepository(sessions)
    repo.set_selection(service, source("1"), 1)
    repo.set_selection(service, source("2"), 2)
    with sessions() as session:
        assert session.get(BotDrawingSelectionRow, (service, "qq_group", "123", "1")).preset_id == one
        assert session.get(BotDrawingSelectionRow, (service, "qq_group", "123", "2")).preset_id == two
    repo.set_selection(service, source("1"), 0)
    with sessions() as session:
        assert session.get(BotDrawingSelectionRow, (service, "qq_group", "123", "1")) is None
        assert session.get(BotDrawingSelectionRow, (service, "qq_group", "123", "2")).preset_id == two
    with pytest.raises(ValueError, match="drawing_scope_denied"):
        repo.set_selection(service, source("1", "456"), 1)


def test_ai_save_preserves_drawing_modes_for_retained_and_removed_chat_groups(sessions):
    with sessions.begin() as session:
        service = setup_scope(session)
        preset = add_preset(session, service)
        session.add(BotDrawingGroupModeRow(service_id=service, group_id="123", mode="r18"))
        session.add(BotDrawingSelectionRow(service_id=service, scope_kind="qq_group", target_id="123",
            sender_id="1", preset_id=preset))
    ai = BotAiRepository(sessions, FernetSecretCipher("drawing-test-key-with-at-least-32-characters"))
    ai.save_settings(service, AiSettingsWrite(vision_enabled=True, groups=["123"]),
                     expected_version=1, operation_id="retain")
    with sessions() as session:
        assert session.get(BotDrawingGroupModeRow, (service, "123")).mode == "r18"
        assert session.get(BotDrawingSelectionRow, (service, "qq_group", "123", "1")) is not None
    ai.save_settings(service, AiSettingsWrite(vision_enabled=True, groups=[]),
                     expected_version=2, operation_id="remove")
    with sessions() as session:
        assert session.get(BotDrawingGroupModeRow, (service, "123")).mode == "r18"
        assert session.get(BotDrawingSelectionRow, (service, "qq_group", "123", "1")) is not None


def frozen_workflow(with_negative=False):
    graph = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
             "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "placeholder"}}}
    if with_negative:
        graph["3"] = {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}
    bindings = InputBindings(positive=PortBinding(node_id="1", input_name="text"),
                             negative=PortBinding(node_id="3", input_name="text") if with_negative else None,
                             output=OutputBinding(node_id="2"))
    result = ValidatedWorkflow("txt2img", graph, graph, bindings, _hash(graph), _hash(graph), "test",
        (AssetDependency("3", "lora_name", "loras", "character.safetensors"),),
        PortBinding(node_id="2", input_name="filename_prefix"), "")
    return replace(result, snapshot_hash=_hash(_snapshot(result), limit=MAX_GRAPH_BYTES * 3))


def test_workflow_versions_are_frozen_and_number_is_stable(sessions):
    with sessions.begin() as session:
        service = setup_scope(session)
    repo = BotDrawingRepository(sessions)
    first = repo.save_preset(service, "角色", frozen_workflow(), {"小猫": "character.safetensors"},
                             enabled=True, preset_id=None, expected_version=0, operation_id="new")
    replay = repo.save_preset(service, "角色", frozen_workflow(), {"小猫": "character.safetensors"},
                              enabled=True, preset_id=None, expected_version=0, operation_id="new")
    assert replay == first and first.number == 1 and first.revision == 1
    assert repo.get_preset(service, first.id)["bindings"] == frozen_workflow().bindings.model_dump(mode="json")
    assert repo.get_workflow(service, first.id).snapshot_hash == frozen_workflow().snapshot_hash
    second = repo.save_preset(service, "角色", frozen_workflow(), {"cat": "character.safetensors"},
                              enabled=True, preset_id=first.id, expected_version=1, operation_id="edit")
    assert second.id == first.id and second.number == 1 and second.revision == 2
    with sessions() as session:
        assert session.get(BotDrawingRevisionRow, (first.id, 1)).snapshot_hash == frozen_workflow().snapshot_hash
        assert session.get(BotDrawingRevisionRow, (first.id, 2)) is not None
    with pytest.raises(ValueError, match="drawing_preset_conflict"):
        repo.save_preset(service, "角色", frozen_workflow(), {}, enabled=True, preset_id=first.id,
                         expected_version=1, operation_id="stale")
    with pytest.raises(ValueError, match="drawing_lora_invalid"):
        repo.save_preset(service, "其他", frozen_workflow(), {"cat": "missing.safetensors"},
                         enabled=True, preset_id=None, expected_version=0, operation_id="invalid")


def ready_for_draw(sessions):
    with sessions.begin() as session:
        service = setup_scope(session)
        preset = add_preset(session, service)
        session.add(BotDrawingSettingsRow(service_id=service, enabled=True, row_version=1, applied_version=1, resource_applied_version=1,
                                          default_text_preset_id=preset))
        session.flush()
        session.add(BotDrawingScopeRow(service_id=service, group_id='123'))
    return service, preset


def test_group_workflow_scope_defaults_open_and_restricted_groups_require_selection(sessions):
    service, first = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    initial = repo.list_group_modes(service)[0]
    assert initial['mode'] == 'green' and initial['restrict_workflows'] is False
    with sessions.begin() as session:
        second = add_preset(session, service, 2)
    intent = PromptIntent(mode='direct', positive='cat')
    assert repo.prepare_route(service, request, intent, 'txt2img', workflow_number=2).decision.status == 'selected'
    assert repo.save_group_mode(service, '123', 'r18', expected_version=0, operation_id='restrict',
        restrict_workflows=True, allowed_preset_ids=[first]) == 1
    current = repo.list_group_modes(service)[0]
    assert current['mode'] == 'r18' and current['restrict_workflows'] is True
    assert current['allowed_preset_ids'] == [str(first)]
    assert [item['id'] for item in repo.list_scope_presets(service, request)] == [str(first)]
    with pytest.raises(ValueError, match='drawing_group_preset_denied'):
        repo.prepare_route(service, request, intent, 'txt2img', workflow_number=2)
    with pytest.raises(ValueError, match='drawing_group_preset_denied'):
        repo.set_selection(service, request, 2)
    with sessions.begin() as session:
        third = add_preset(session, service, 3)
    with pytest.raises(ValueError, match='drawing_group_preset_denied'):
        repo.prepare_route(service, request, intent, 'txt2img', workflow_number=3)
    assert repo.save_group_mode(service, '123', 'green', expected_version=1, operation_id='open',
        restrict_workflows=False, allowed_preset_ids=[first]) == 2
    assert repo.prepare_route(service, request, intent, 'txt2img', workflow_number=3).decision.workflow.id == third
    with sessions.begin() as session:
        fourth = add_preset(session, service, 4)
    assert repo.prepare_route(service, request, intent, 'txt2img', workflow_number=4).decision.workflow.id == fourth


def test_group_workflow_scope_rechecked_after_preparing_and_old_mode_writer_preserves_it(sessions):
    service, first = ready_for_draw(sessions)
    with sessions.begin() as session:
        second = add_preset(session, service, 2)
    repo, request = BotDrawingRepository(sessions), source()
    intent = PromptIntent(mode='direct', positive='cat')
    prepared = repo.prepare_route(service, request, intent, 'txt2img', workflow_number=2)
    assert repo.save_group_mode(service, '123', 'green', expected_version=0, operation_id='restrict',
        restrict_workflows=True, allowed_preset_ids=[first]) == 1
    with pytest.raises(ValueError, match='drawing_route_changed'):
        repo.admit(service, request, '456', 'local', prepared, positive='cat', negative='', seed=1)
    assert repo.save_group_mode(service, '123', 'r18', expected_version=1, operation_id='legacy') == 2
    current = repo.list_group_modes(service)[0]
    assert current['restrict_workflows'] is True and current['allowed_preset_ids'] == [str(first)]
    with pytest.raises(ValueError, match='drawing_group_preset_denied'):
        repo.prepare_route(service, request, intent, 'txt2img', workflow_number=2)


def test_group_workflow_scope_isolated_from_other_groups_private_and_p2p(sessions):
    service, first = ready_for_draw(sessions)
    with sessions.begin() as session:
        edit = add_preset(session, service, 2, kind='image_edit')
        session.get(BotDrawingSettingsRow, service).default_edit_preset_id = edit
        session.get(BotDrawingSettingsRow, service).allow_private = True
        session.add(BotDrawingScopeRow(service_id=service, group_id='456'))
    repo = BotDrawingRepository(sessions)
    intent = PromptIntent(mode='direct', positive='cat')
    repo.save_group_mode(service, '123', 'green', expected_version=0, operation_id='restrict',
                         restrict_workflows=True, allowed_preset_ids=[first])
    with pytest.raises(ValueError, match='drawing_group_preset_denied'):
        repo.prepare_route(service, source(), intent, 'image_edit', force_default=True)
    assert repo.prepare_route(service, source(group='456'), intent, 'image_edit', force_default=True).decision.workflow.id == edit
    private = CommandSource(message_id='101', scope_kind='qq_private', target_id='1', sender_id='1')
    assert repo.prepare_route(service, private, intent, 'image_edit', force_default=True).decision.workflow.id == edit


def test_group_workflow_write_is_atomic_and_independent_of_chat_scopes(sessions):
    service, first = ready_for_draw(sessions)
    with sessions.begin() as session:
        foreign = add_preset(session, add_service(session, 'other'))
    repo = BotDrawingRepository(sessions)
    args = dict(expected_version=0, operation_id='restrict', restrict_workflows=True, allowed_preset_ids=[first])
    assert repo.save_group_mode(service, '123', 'r18', **args) == 1
    assert repo.save_group_mode(service, '123', 'r18', **args) == 1
    with pytest.raises(ValueError, match='idempotency_conflict'):
        repo.save_group_mode(service, '123', 'green', **args)
    with pytest.raises(ValueError, match='drawing_group_mode_conflict'):
        repo.save_group_mode(service, '123', 'green', expected_version=0, operation_id='stale',
                             restrict_workflows=True, allowed_preset_ids=[])
    with pytest.raises(ValueError, match='drawing_group_presets_invalid'):
        repo.save_group_mode(service, '123', 'green', expected_version=1, operation_id='foreign',
                             restrict_workflows=True, allowed_preset_ids=[foreign])
    with pytest.raises(ValueError, match='drawing_scope_denied'):
        repo.save_group_mode(service, '999', 'green', expected_version=0, operation_id='unauthorized',
                             restrict_workflows=True, allowed_preset_ids=[first])
    ai = BotAiRepository(sessions, FernetSecretCipher('drawing-test-key-with-at-least-32-characters'))
    ai.save_settings(service, AiSettingsWrite(groups=['123']), expected_version=1, operation_id='retain')
    assert repo.list_group_modes(service)[0]['allowed_preset_ids'] == [str(first)]
    ai.save_settings(service, AiSettingsWrite(groups=[]), expected_version=2, operation_id='remove')
    with sessions() as session:
        assert session.scalar(text('select count(*) from bot_drawing_group_presets where service_id=:sid'), {'sid':service}) == 1
    ai.save_settings(service, AiSettingsWrite(groups=['123']), expected_version=3, operation_id='restore')
    group = repo.list_group_modes(service)[0]
    assert group['mode'] == 'r18' and group['restrict_workflows'] is True and group['allowed_preset_ids'] == [str(first)]


def test_concurrent_group_workflow_changes_do_not_overwrite_each_other(sessions):
    service, first = ready_for_draw(sessions)
    barrier = Barrier(2)
    def save(index):
        repo = BotDrawingRepository(sessions)
        barrier.wait(timeout=5)
        try:
            return repo.save_group_mode(service, '123', 'green', expected_version=0,
                operation_id=f'concurrent-{index}', restrict_workflows=True, allowed_preset_ids=[first])
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, range(2)))
    assert sorted(str(item) for item in results) == ['1', 'drawing_group_mode_conflict']


def test_group_workflow_list_uses_fixed_queries_at_scope_and_preset_limits(sessions):
    with sessions.begin() as session:
        service = setup_scope(session)
        group_ids = ['123', *[str(2000 + index) for index in range(99)]]
        session.add(BotDrawingSettingsRow(service_id=service, enabled=True, row_version=1, applied_version=1))
        session.flush()
        session.add_all([BotDrawingScopeRow(service_id=service, group_id=group) for group in group_ids])
        presets = [add_preset(session, service, number) for number in range(1, 21)]
        session.add_all([BotDrawingGroupModeRow(service_id=service, group_id=group, mode='green',
                                                restrict_workflows=True) for group in group_ids])
        session.flush()
        session.add_all([BotDrawingGroupPresetRow(service_id=service, group_id=group, preset_id=preset)
                         for group in group_ids for preset in presets])
    calls = []
    engine = sessions.kw['bind']
    def record(_conn, _cursor, statement, *_args):
        if statement.lstrip().lower().startswith('select'): calls.append(statement)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        groups = BotDrawingRepository(sessions).list_group_modes(service)
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert len(groups) == 100 and all(len(group['allowed_preset_ids']) == 20 for group in groups)
    assert len(calls) <= 3


def test_explicit_workflow_does_not_change_saved_selection_and_is_idempotent(sessions):
    service, first = ready_for_draw(sessions)
    with sessions.begin() as session:
        second = add_preset(session, service, 2)
    repo, request = BotDrawingRepository(sessions), source()
    repo.set_selection(service, request, 1)
    intent = PromptIntent(mode="direct", positive="cat")
    prepared = repo.prepare_route(service, request, intent, "txt2img", workflow_number=2)
    assert prepared.decision.status == "selected" and prepared.decision.workflow.id == second
    task = repo.admit(service, request, "456", "local", prepared, positive="cat", negative="", seed=1)
    assert repo.admit(service, request, "456", "local", prepared, positive="cat", negative="", seed=1) == task
    with sessions() as session:
        assert session.get(BotDrawingSelectionRow, (service, "qq_group", "123", "1")).preset_id == first
        assert session.get(BotDrawingTaskRow, task).preset_id == second


def test_explicit_workflow_rechecks_preset_but_ignores_personal_selection_changes(sessions):
    service, _ = ready_for_draw(sessions)
    with sessions.begin() as session:
        second = add_preset(session, service, 2)
    repo, request = BotDrawingRepository(sessions), source()
    repo.set_selection(service, request, 1)
    intent = PromptIntent(mode="direct", positive="cat")
    prepared = repo.prepare_route(service, request, intent, "txt2img", workflow_number=2)
    repo.set_selection(service, request, 0)
    with sessions.begin() as session:
        row = session.get(BotDrawingPresetRow, second)
        row.enabled = False
        row.row_version += 1
    with pytest.raises(ValueError, match="drawing_route_changed"):
        repo.admit(service, request, "456", "local", prepared, positive="cat", negative="", seed=1)
    with sessions.begin() as session:
        row = session.get(BotDrawingPresetRow, second)
        row.enabled = True
        row.row_version += 1
    prepared = repo.prepare_route(service, request, intent, "txt2img", workflow_number=2)
    repo.set_selection(service, request, 1)
    task = repo.admit(service, request, "456", "local", prepared, positive="cat", negative="", seed=1)
    with sessions() as session:
        assert session.get(BotDrawingTaskRow, task).preset_id == second
        assert session.get(BotDrawingSelectionRow, (service, "qq_group", "123", "1")) is not None


def test_explicit_missing_or_wrong_kind_does_not_fall_back(sessions):
    service, preset = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    intent = PromptIntent(mode="direct", positive="cat")
    assert repo.prepare_route(service, request, intent, "txt2img", workflow_number=999).decision.status == "selection_unavailable"
    assert repo.prepare_route(service, request, intent, "image_edit", workflow_number=1).decision.status == "kind_mismatch"
    with pytest.raises(ValueError, match="drawing_scope_denied"):
        repo.prepare_route(service, source(group="456"), intent, "txt2img", workflow_number=1)
    with sessions.begin() as session:
        session.get(BotDrawingPresetRow, preset).enabled = False
    assert repo.prepare_route(service, request, intent, "txt2img", workflow_number=1).decision.status == "selection_unavailable"


def test_route_is_rechecked_after_prompt_expansion_and_duplicate_delivery_is_idempotent(sessions):
    service, preset = ready_for_draw(sessions)
    repo = BotDrawingRepository(sessions)
    intent = PromptIntent(mode="direct", positive="cat")
    request = source()
    prepared = repo.prepare_route(service, request, intent, "txt2img")
    with sessions.begin() as session:
        row = session.get(BotDrawingPresetRow, preset)
        row.enabled = False
        row.row_version += 1
    with pytest.raises(ValueError, match="drawing_route_changed"):
        repo.admit(service, request, "456", "local", prepared, positive="cat", negative="", seed=1)
    with sessions.begin() as session:
        row = session.get(BotDrawingPresetRow, preset)
        row.enabled = True
        row.row_version += 1
    prepared = repo.prepare_route(service, request, intent, "txt2img")
    first = repo.admit(service, request, "456", "local", prepared, positive="cat", negative="", seed=1)
    second = repo.admit(service, request, "456", "local", prepared, positive="cat", negative="", seed=1)
    assert first == second
    with sessions() as session:
        assert session.get(BotDrawingTaskRow, first).preset_revision == 1


def test_chat_profile_switch_during_expansion_does_not_change_drawing_route(sessions):
    service, _ = ready_for_draw(sessions)
    first, second = uuid4(), uuid4()
    with sessions.begin() as session:
        session.add_all([BotAiChatProfileRow(id=first, service_id=service, name="one", normalized_name="one",
            adapter_kind="openai_chat_completion", model_id="one", base_url="", network_mode="direct",
            supports_vision=False, is_default=True),
            BotAiChatProfileRow(id=second, service_id=service, name="two", normalized_name="two",
            adapter_kind="openai_chat_completion", model_id="two", base_url="", network_mode="direct",
            supports_vision=False, is_default=False)])
    repo, request = BotDrawingRepository(sessions), source()
    intent = PromptIntent(mode="wish")
    prepared = repo.prepare_route(service, request, intent, "txt2img")
    assert prepared.chat_profile_id is None
    with sessions.begin() as session:
        session.add(BotAiSessionProfileRow(service_id=service, scope_kind="qq_group", target_id="123",
            profile_id=second, updated_at=datetime.now(UTC)))
    assert repo.admit(service, request, "456", "local", prepared, positive="", negative="", seed=1, wish_text="cat")


def test_image_edit_input_must_be_owned_by_the_frozen_task(sessions):
    with sessions.begin() as session:
        service = setup_scope(session)
        preset = add_preset(session, service, kind="image_edit")
        session.add(BotDrawingSettingsRow(service_id=service, enabled=True, row_version=1, applied_version=1,
                                          resource_applied_version=1, default_edit_preset_id=preset))
        session.flush()
        session.add(BotDrawingScopeRow(service_id=service, group_id='123'))
    repo, request, task_id = BotDrawingRepository(sessions), source(), uuid4()
    intent = PromptIntent(mode="direct", positive="换背景")
    prepared = repo.prepare_route(service, request, intent, "image_edit", force_default=True)
    with pytest.raises(ValueError, match="drawing_request_invalid"):
        repo.admit(service, request, "456", "local", prepared, positive="换背景", negative="", seed=1,
                   task_id=task_id, input_reference=f"al1s-drawing/{uuid4()}/input.png", input_sha256="a" * 64,
                   input_bytes=100)
    result = repo.admit(service, request, "456", "local", prepared, positive="换背景", negative="", seed=1,
        task_id=task_id, input_reference=f"al1s-drawing/{task_id}/input.png", input_sha256="a" * 64, input_bytes=100)
    assert result == task_id
    with sessions() as session:
        row = session.get(BotDrawingTaskRow, task_id)
        assert row.input_sha256 == "a" * 64 and row.input_bytes == 100


def test_tag_is_a_separate_owned_task_without_drawing_preset(sessions):
    with sessions.begin() as session:
        service = setup_scope(session)
        session.add(BotDrawingSettingsRow(service_id=service, enabled=True, row_version=1, applied_version=1,
                                          resource_applied_version=1))
        session.flush()
        session.add(BotDrawingScopeRow(service_id=service, group_id='123'))
    repo, request, task_id = BotDrawingRepository(sessions), source(), uuid4()
    prepared = repo.prepare_tag(service, request)
    kwargs = dict(task_id=task_id, input_reference=f"al1s-drawing/{task_id}/input.png",
                  input_sha256="a" * 64, input_bytes=100, reference_message_id="456")
    result = repo.admit_tag(service, request, "456", "local", prepared, **kwargs)
    assert result == repo.admit_tag(service, request, "456", "local", prepared, **kwargs) == task_id
    with sessions() as session:
        row = session.get(BotDrawingTaskRow, task_id)
        assert row.operation == "tag" and row.preset_id is None and row.input_sha256 == "a" * 64


def test_concurrent_admission_obeys_endpoint_queue_limit(sessions):
    service, _ = ready_for_draw(sessions)
    repo = BotDrawingRepository(sessions)
    intent = PromptIntent(mode="direct", positive="cat")
    requests = [CommandSource(message_id=str(100 + i), scope_kind="qq_group", target_id="123", sender_id="1")
                 for i in range(7)]
    prepared = [repo.prepare_route(service, request, intent, "txt2img") for request in requests]
    barrier = Barrier(len(requests))

    def admit(item):
        request, route = item
        barrier.wait(timeout=10)
        try:
            return repo.admit(service, request, "456", "local", route, positive="cat", negative="", seed=1)
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        results = list(pool.map(admit, zip(requests, prepared)))
    assert sum(isinstance(value, str) and value == "drawing_queue_full" for value in results) == 1
    assert len({value for value in results if not isinstance(value, str)}) == 6


def test_one_claim_per_endpoint_and_version_checked_transitions(sessions):
    service, _ = ready_for_draw(sessions)
    repo = BotDrawingRepository(sessions)
    intent = PromptIntent(mode="direct", positive="cat")
    requests = [CommandSource(message_id=str(100 + i), scope_kind="qq_group", target_id="123", sender_id=str(i + 1))
                for i in range(2)]
    ids = [repo.admit(service, request, "456", "local", repo.prepare_route(service, request, intent, "txt2img"),
                      positive="cat", negative="", seed=1) for request in requests]
    assert repo.claim_next("local") is None
    for identity in ids:
        repo.mark_registration(identity, expected_version=1, boot_id="boot-1", execution_hash="d" * 64)
    claim = repo.claim_next("local")
    assert claim.id == ids[0] and claim.status == "submitting"
    assert repo.claim_next("local") is None
    with pytest.raises(ValueError, match="drawing_task_conflict"):
        repo.transition(claim.id, "submitting", "running", expected_version=claim.row_version - 1)
    with pytest.raises(ValueError, match="drawing_task_not_registered"):
        repo.transition(claim.id, "submitting", "running", expected_version=claim.row_version)
    started = repo.begin_submit(claim.id, expected_version=claim.row_version)
    with pytest.raises(ValueError, match="drawing_task_conflict"):
        repo.begin_submit(claim.id, expected_version=started)
    version = repo.transition(claim.id, "submitting", "running", expected_version=started)
    version = repo.reserve_output(claim.id, expected_version=version, size=128, checksum="e" * 64)
    repo.transition(claim.id, "running", "succeeded", expected_version=version,
                    output_reference=f"al1s-drawing/{claim.id}/output.png", output_sha256="e" * 64, output_bytes=128)
    assert repo.claim_next("local").id == ids[1]


def test_secret_cleanup_preserves_resource_token_referenced_by_drawing_settings(sessions):
    secret_id = uuid4()
    with sessions.begin() as session:
        service = add_service(session, "one")
        session.add(EncryptedSecretRow(id=secret_id, purpose="drawing-resource", ciphertext="encrypted",
                                      key_id="test", row_version=1))
        session.flush()
        session.add(BotDrawingSettingsRow(service_id=service, resource_secret_id=secret_id))
    with sessions.begin() as session:
        assert PostgresSecretRepository(session).delete_if_unreferenced(secret_id) is False
    with sessions() as session:
        assert session.get(EncryptedSecretRow, secret_id) is not None


def test_vision_jobs_preserve_legacy_and_managed_recovery_state(sessions):
    with sessions.begin() as session:
        service = add_service(session, "one")
    repository = AiHistoryRepository(sessions)
    now = datetime.now(UTC).timestamp()
    legacy = {"prompt_id": str(uuid4()), "created_at": now}
    managed = {"prompt_id": str(uuid4()), "created_at": now + 1, "boot_id": "boot-1",
        "execution_hash": "a" * 64, "registration_state": "confirmed", "transport_started": True}
    rejected = {"prompt_id": str(uuid4()), "created_at": now + 2, "boot_id": "boot-1",
        "execution_hash": "b" * 64, "registration_state": "rejected", "transport_started": True}
    endpoint = "http://host.docker.internal:8188"
    repository.save_vision_jobs(service, endpoint, [legacy, managed, rejected])
    assert repository.vision_jobs(service, endpoint) == [legacy, managed, rejected]


def test_resource_token_is_encrypted_and_reused_by_vision(sessions):
    with sessions.begin() as session:
        service = add_service(session, "one")
    cipher = FernetSecretCipher("drawing-test-key-with-at-least-32-characters")
    drawing = BotDrawingRepository(sessions, cipher=cipher)
    token = "resource-token-" + "a" * 40
    drawing.bind_resource_token(service, token)
    ai = BotAiRepository(sessions, cipher)
    assert ai.drawing_resource_credentials(service) == (token, True)
    with sessions() as session:
        settings = session.get(BotDrawingSettingsRow, service)
        secret = session.get(EncryptedSecretRow, settings.resource_secret_id)
        assert secret.ciphertext != token and token not in secret.ciphertext


def test_admin_lists_only_current_authorized_groups_and_bounded_preset_metadata(sessions):
    with sessions.begin() as session:
        service = setup_scope(session)
        add_preset(session, service)
        session.add(BotDrawingSettingsRow(service_id=service, enabled=True, row_version=1, applied_version=1))
        session.flush()
        session.add(BotDrawingScopeRow(service_id=service, group_id='123'))
        session.add(BotDrawingGroupModeRow(service_id=service, group_id="123", mode="r18"))
    repo = BotDrawingRepository(sessions)
    statements = []
    engine = sessions.kw["bind"]
    def record(_connection, _cursor, statement, _parameters, _context, _many): statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        groups = repo.list_group_modes(service)
        presets = repo.list_presets(service)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert groups == [{"group_id": "123", "mode": "r18", "row_version": 1,
                       "restrict_workflows": False, "allowed_preset_ids": []}]
    assert len(presets) == 1 and presets[0]["number"] == 1
    assert all("source_graph" not in statement and "execution_template" not in statement for statement in statements)
    assert len(statements) <= 5


def test_applied_residency_is_still_reconciled_after_comfy_restarts(sessions):
    with sessions.begin() as session:
        service = setup_scope(session)
        session.add(BotDrawingSettingsRow(service_id=service, enabled=True, keep_drawing_warm=True,
                                          row_version=4, applied_version=4))
    assert BotDrawingRepository(sessions).next_settings() == (service, 4)


def test_dependency_check_freezes_actual_model_hash_and_requires_preview(sessions):
    with sessions.begin() as session:
        service = setup_scope(session)
    repo = BotDrawingRepository(sessions)
    preset = repo.save_preset(service, "角色", frozen_workflow(), {}, enabled=True,
                              preset_id=None, expected_version=0, operation_id="new")
    dependencies = {"adapter": "test", "boot_id": "boot-1", "nodes": [
        {"name": "CLIPTextEncode", "sha256": "f" * 64}, {"name": "SaveImage", "sha256": "f" * 64}],
        "assets": [{"kind": "loras", "name": "character.safetensors", "size": 123, "sha256": "e" * 64}]}
    assert repo.record_check(service, preset.id, preset.revision, preset.row_version, dependencies,
                             operation_id="check-1") == "needs_preview"
    with sessions() as session:
        row = session.get(BotDrawingRevisionRow, (preset.id, preset.revision))
        assert row.dependency_manifest == dependencies and len(row.dependency_signature) == 64
        assert row.validation_state == "needs_preview"
    assert repo.check_receipt(service, preset.id, preset.row_version, "check-1") == "needs_preview"
    assert repo.record_check(service, preset.id, preset.revision, preset.row_version, dependencies,
                             operation_id="check-1") == "needs_preview"
    dependencies["assets"][0]["name"] = "other.safetensors"
    with pytest.raises(ValueError, match="drawing_dependency_mismatch"):
        repo.record_check(service, preset.id, preset.revision, preset.row_version, dependencies)


def test_send_checks_current_group_mode_and_exact_audited_image_before_transport(sessions):
    from al1s.bot_drawing.moderation import MODEL_REVISION, POLICY_VERSION
    service, _ = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    intent = PromptIntent(mode="direct", positive="cat")
    task = repo.admit(service, request, "456", "local", repo.prepare_route(service, request, intent, "txt2img"),
                      positive="cat", negative="", seed=1)
    with sessions.begin() as session:
        session.add(BotDrawingGroupModeRow(service_id=service, group_id="123", mode="r18", row_version=1))
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.reply_kind = "succeeded", "image"
        row.output_reference, row.output_sha256, row.output_bytes = f"al1s-drawing/{task}/output.png", "e" * 64, 128
        row.audit_status, row.audit_mode = "not_required", "r18"
    assert repo.start_send(task, "result")
    with sessions.begin() as session:
        session.get(BotDrawingGroupModeRow, (service, "123")).mode = "green"
    assert not repo.mark_transport_started(task, "result", "e" * 64)
    repo.finish_send(task, "result", "failed", 0, None)
    assert repo.requeue_result(task, "image")
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.audit_status = "allow"
        row.audit_model_revision, row.audit_policy_version = MODEL_REVISION, POLICY_VERSION
        row.audit_sha256 = "e" * 64
    assert repo.start_send(task, "result")
    assert not repo.mark_transport_started(task, "result", "f" * 64)
    assert repo.mark_transport_started(task, "result", "e" * 64)
    assert not repo.mark_transport_started(task, "result", "e" * 64)
    repo.finish_send(task, "result", "sent", 1, "777")
    assert not repo.requeue_result(task, "image")
    with sessions() as session:
        row = session.get(BotDrawingTaskRow, task)
        assert row.send_status == "sent" and row.result_receipt == "777"


@pytest.mark.parametrize("change", ["groups", "prompt_model"])
@pytest.mark.parametrize("applied", [False, True])
def test_accepted_task_survives_unrelated_config_changes(sessions, change, applied):
    from al1s.api.bot_drawing import PromptProviderWrite
    from al1s.bot_drawing.moderation import MODEL_REVISION, POLICY_VERSION

    service, preset = ready_for_draw(sessions)
    repo = BotDrawingRepository(
        sessions, cipher=FernetSecretCipher("drawing-test-key-with-at-least-32-characters")
    )
    request = source()
    task = repo.admit(
        service,
        request,
        "456",
        "local",
        repo.prepare_route(
            service, request, PromptIntent(mode="direct", positive="cat"), "txt2img"
        ),
        positive="cat",
        negative="",
        seed=1,
    )
    if change == "groups":
        repo.save_settings(
            service,
            enabled=True,
            keep_drawing_warm=False,
            default_text_preset_id=preset,
            default_edit_preset_id=None,
            groups=["123", "321"],
            expected_version=1,
            operation_id="add-group",
        )
    else:
        provider = PromptProviderWrite(
            name="新模型",
            adapter_kind="openai_chat_completion",
            model_id="new-drawing-model",
            secret_input="synthetic-drawing-key",
        )
        repo.save_prompt_model(
            service, provider, profile_id=None, expected_version=0, operation_id="change-model"
        )
    if applied and change == "groups":
        assert repo.mark_applied(service, 2)

    assert repo.current_mode(service, request, 1) == "green"
    assert repo.allowed(service, request, 1)
    assert repo.start_send(task, "notice")
    assert repo.mark_transport_started(task, "notice", None)
    assert repo.finish_send(task, "notice", "sent", 1, "notice-receipt")
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.reply_kind, row.send_status = "succeeded", "image", "ready"
        row.output_reference, row.output_sha256, row.output_bytes = (
            f"al1s-drawing/{task}/output.png",
            "e" * 64,
            128,
        )
        row.audit_status, row.audit_sha256 = "allow", "e" * 64
        row.audit_model_revision, row.audit_policy_version = MODEL_REVISION, POLICY_VERSION
    assert repo.start_send(task, "result")
    assert repo.mark_transport_started(task, "result", "e" * 64)
    assert repo.finish_send(task, "result", "sent", 1, "result-receipt")


@pytest.mark.parametrize("applied", [False, True])
def test_result_delivery_after_prompt_model_change_uses_the_saved_image(
    sessions, tmp_path, applied
):
    import asyncio
    from io import BytesIO

    from PIL import Image

    from al1s.bot_ai.sender import AiSender
    from al1s.api.bot_drawing import PromptProviderWrite
    from al1s.bot_drawing.media import DrawingMediaStore
    from al1s.bot_drawing.moderation import MODEL_REVISION, POLICY_VERSION
    from al1s.bot_drawing.runner import DrawingTaskRunner

    service, preset = ready_for_draw(sessions)
    repo = BotDrawingRepository(
        sessions, cipher=FernetSecretCipher("drawing-test-key-with-at-least-32-characters")
    )
    request = source()
    task = repo.admit(
        service,
        request,
        "456",
        "local",
        repo.prepare_route(
            service, request, PromptIntent(mode="direct", positive="cat"), "txt2img"
        ),
        positive="cat",
        negative="",
        seed=1,
    )
    media = DrawingMediaStore(tmp_path)
    output = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(output, format="PNG")
    body = output.getvalue()
    reference, checksum = media.put(task, "output", body)
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.reply_kind, row.send_status = "succeeded", "image", "ready"
        row.output_reference, row.output_sha256, row.output_bytes = reference, checksum, len(body)
        row.audit_status, row.audit_sha256 = "allow", checksum
        row.audit_model_revision, row.audit_policy_version = MODEL_REVISION, POLICY_VERSION
    provider = PromptProviderWrite(
        name="新模型",
        adapter_kind="openai_chat_completion",
        model_id="new-drawing-model",
        secret_input="synthetic-drawing-key",
    )
    repo.save_prompt_model(service, provider, profile_id=None, expected_version=0, operation_id="change-model")
    repo.save_settings(service, enabled=True, keep_drawing_warm=False, default_text_preset_id=preset,
                       default_edit_preset_id=None, groups=["123", "321"], expected_version=1, operation_id="add-group")
    if applied:
        assert repo.mark_applied(service, 2)
    assert repo.next_delivery("local") == (task, "result")

    class Client:
        def __init__(self):
            self.parts = []

        def send_segments(self, _source, parts, **_kwargs):
            self.parts.extend(parts)
            return "synthetic-receipt"

    client = Client()
    runner = DrawingTaskRunner(
        repo, None, media, None, AiSender(repo, tmp_path, interval=0), lambda _: client
    )
    asyncio.run(runner.deliver(task, "result"))
    assert len(client.parts) == 1 and client.parts[0]["body"] == body
    with sessions() as session:
        row = session.get(BotDrawingTaskRow, task)
        assert row.send_status == "sent" and row.result_receipt == "synthetic-receipt"


@pytest.mark.parametrize("change", ["revoke_group", "disable_drawing"])
def test_pending_config_change_still_revokes_delivery(sessions, change):
    service, preset = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    task = repo.admit(
        service,
        request,
        "456",
        "local",
        repo.prepare_route(
            service, request, PromptIntent(mode="direct", positive="cat"), "txt2img"
        ),
        positive="cat",
        negative="",
        seed=1,
    )
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.reply_kind, row.send_status = "failed", "failure_notice", "ready"
    repo.save_settings(
        service,
        enabled=change != "disable_drawing",
        keep_drawing_warm=False,
        default_text_preset_id=preset,
        default_edit_preset_id=None,
        groups=[] if change == "revoke_group" else ["123"],
        expected_version=1,
        operation_id="revoke",
    )
    assert not repo.allowed(service, request, 1)
    assert repo.next_delivery("local") is None
    assert repo.start_send(task, "result")
    assert not repo.mark_transport_started(task, "result", None)


@pytest.mark.parametrize(
    "status,submitted", [("queued", False), ("submitting", False), ("running", True)]
)
def test_config_change_does_not_block_owned_task_cancellation(sessions, status, submitted):
    import asyncio

    from al1s.bot_drawing.runner import DrawingTaskRunner

    service, preset = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    task = repo.admit(
        service,
        request,
        "456",
        "local",
        repo.prepare_route(
            service, request, PromptIntent(mode="direct", positive="cat"), "txt2img"
        ),
        positive="cat",
        negative="",
        seed=1,
    )
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.prompt_transport_started = status, submitted
        if submitted:
            row.resource_registration_state, row.boot_id, row.execution_hash = (
                "confirmed",
                "boot-1",
                "a" * 64,
            )
    repo.save_settings(
        service,
        enabled=True,
        keep_drawing_warm=False,
        default_text_preset_id=preset,
        default_edit_preset_id=None,
        groups=["123", "321"],
        expected_version=1,
        operation_id="add-group",
    )

    assert repo.request_cancel(service, task, operation_id="cancel") == (
        "cancelling" if submitted else "cancelled"
    )
    if submitted:
        calls = []

        class Resource:
            async def state(self):
                return {"boot_id": "boot-1"}

            async def cancel(self, prompt, *_):
                calls.append(prompt)
                return "cancelled"

            async def release(self, *_):
                return "released"

            async def delete_history(self, *_):
                return True

            async def close(self):
                pass

        class Configuration:
            async def client(self, _, **_kwargs):
                return Resource()

        runner = DrawingTaskRunner(repo, Configuration(), None, None, None, lambda _: None)
        assert asyncio.run(runner.advance(task)) == "cancelled"
        assert calls == [repo.task_for_worker(task)["prompt_id"]]
    assert repo.allowed(service, request, 1)
    assert repo.start_send(task, "result")
    assert repo.mark_transport_started(task, "result", None)
    with sessions() as session:
        row = session.get(BotDrawingTaskRow, task)
        assert row.generation_status == "cancelled" and row.cleanup_status == "done"


def test_revoked_group_does_not_block_other_task_delivery(sessions):
    service, preset = ready_for_draw(sessions)
    with sessions.begin() as session:
        session.add(BotDrawingScopeRow(service_id=service, group_id="321"))
    repo = BotDrawingRepository(sessions)
    first, second = source(group="123"), source(group="321")
    intent = PromptIntent(mode="direct", positive="cat")
    tasks = [
        repo.admit(
            service,
            request,
            "456",
            "local",
            repo.prepare_route(service, request, intent, "txt2img"),
            positive="cat",
            negative="",
            seed=1,
        )
        for request in (first, second)
    ]
    with sessions.begin() as session:
        session.delete(session.get(BotDrawingScopeRow, (service, "123")))
        for task in tasks:
            row = session.get(BotDrawingTaskRow, task)
            row.generation_status, row.reply_kind, row.send_status = (
                "failed",
                "failure_notice",
                "ready",
            )
            row.notice_status = "sent"
        older = [make_task(service, preset, str(i + 1000), str(i + 1000)) for i in range(1000)]
        for row in older:
            row.endpoint_key = "local"
            row.generation_status, row.reply_kind, row.send_status = (
                "failed",
                "failure_notice",
                "ready",
            )
            row.notice_status = "sent"
            row.created_at = datetime.now(UTC) - timedelta(minutes=1)
        session.add_all(older)
    assert not repo.allowed(service, first, 1)
    calls = []

    def record(_conn, _cursor, statement, *_args):
        if statement.lstrip().lower().startswith("select"):
            calls.append(statement)

    engine = sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert repo.next_delivery("local") == (tasks[1], "result")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(calls) == 1


def test_disabled_preset_cannot_send_old_image_after_config_change(sessions):
    from al1s.bot_drawing.moderation import MODEL_REVISION, POLICY_VERSION

    service, preset = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    task = repo.admit(
        service,
        request,
        "456",
        "local",
        repo.prepare_route(
            service, request, PromptIntent(mode="direct", positive="cat"), "txt2img"
        ),
        positive="cat",
        negative="",
        seed=1,
    )
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.reply_kind, row.send_status = "succeeded", "image", "ready"
        row.output_reference, row.output_sha256, row.output_bytes = (
            f"al1s-drawing/{task}/output.png",
            "e" * 64,
            128,
        )
        row.audit_status, row.audit_sha256 = "allow", "e" * 64
        row.audit_model_revision, row.audit_policy_version = MODEL_REVISION, POLICY_VERSION
        session.get(BotDrawingPresetRow, preset).enabled = False
    assert repo.next_delivery("local") is None
    assert repo.start_send(task, "result")
    assert not repo.mark_transport_started(task, "result", "e" * 64)


@pytest.mark.parametrize(
    "case", ["missing_idle", "missing_busy", "job_present", "changed_config", "invalid_queue"]
)
def test_cancelled_task_recovers_safely_after_comfy_restart(sessions, tmp_path, case):
    import asyncio

    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_drawing.runner import DrawingTaskRunner

    service, _ = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    task = repo.admit(
        service,
        request,
        "456",
        "local",
        repo.prepare_route(
            service, request, PromptIntent(mode="direct", positive="cat"), "txt2img"
        ),
        positive="cat",
        negative="",
        seed=1,
    )
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.prompt_transport_started = "cancelling", True
        row.resource_registration_state, row.boot_id, row.execution_hash = (
            "confirmed",
            "old-boot",
            "a" * 64,
        )
        if case == "changed_config":
            settings = session.get(BotDrawingSettingsRow, service)
            settings.row_version = settings.applied_version = 2
    calls = []

    class Resource:
        async def state(self):
            return {"boot_id": "new-boot"}

        async def _json(self, method, path, **_kwargs):
            calls.append((method, path))
            if path.startswith("/api/jobs/"):
                return {"status": "in_progress"} if case == "job_present" else None
            if case == "invalid_queue":
                return {"queue_running": None, "queue_pending": []}
            return {
                "queue_running": ["another-job"] if case == "missing_busy" else [],
                "queue_pending": [],
            }

        async def close(self):
            pass

    class Configuration:
        async def client(self, _, **_kwargs):
            return Resource()

    queue = GpuQueue()
    runner = DrawingTaskRunner(
        repo, Configuration(), None, None, None, lambda _: None, gpu_queue=queue
    )

    async def run():
        queue.reserve(str(task))
        return await runner.advance(task)

    result = asyncio.run(run())
    with sessions() as session:
        row = session.get(BotDrawingTaskRow, task)
        if case in {"missing_idle", "changed_config"}:
            assert result == "cancelled"
            assert row.generation_status == "cancelled" and row.cleanup_status == "done"
            assert row.send_status == "ready" and queue.active is None
        else:
            assert result == "unknown"
            assert row.generation_status == "cancelling" and row.cleanup_status != "done"
            assert queue.active == str(task)
    assert all(method == "GET" for method, _ in calls)
    if case == "missing_idle":
        from al1s.bot_ai.sender import AiSender

        class Client:
            def __init__(self):
                self.parts = []

            def send_segments(self, _source, parts, **_kwargs):
                self.parts.extend(parts)
                return "synthetic-receipt"

        client = Client()
        runner.sender = AiSender(repo, tmp_path, interval=0)
        runner.client_for_service = lambda _: client
        asyncio.run(runner.deliver(task, "result"))
        assert client.parts == [{"type": "text", "text": "绘图任务已取消。"}]
        with sessions() as session:
            row = session.get(BotDrawingTaskRow, task)
            assert row.send_status == "sent" and row.result_receipt == "synthetic-receipt"


def test_restart_cancellation_rechecks_config_in_same_transaction(sessions):
    import asyncio

    from al1s.bot_drawing.runner import DrawingTaskRunner

    service, _ = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    task = repo.admit(
        service,
        request,
        "456",
        "local",
        repo.prepare_route(
            service, request, PromptIntent(mode="direct", positive="cat"), "txt2img"
        ),
        positive="cat",
        negative="",
        seed=1,
    )
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.prompt_transport_started = "cancelling", True
        row.comfy_endpoint = None
        row.resource_registration_state, row.boot_id, row.execution_hash = (
            "confirmed",
            "old-boot",
            "a" * 64,
        )

    class Resource:
        async def state(self):
            return {"boot_id": "new-boot"}

        async def _json(self, _method, path, **_kwargs):
            if path.startswith("/api/jobs/"):
                return None
            with sessions.begin() as session:
                settings = session.get(BotDrawingSettingsRow, service)
                settings.row_version = settings.applied_version = 2
            return {"queue_running": [], "queue_pending": []}

        async def close(self):
            pass

    class Configuration:
        async def client(self, _, **_kwargs):
            return Resource()

    runner = DrawingTaskRunner(repo, Configuration(), None, None, None, lambda _: None)
    with pytest.raises(ValueError, match="drawing_task_conflict"):
        asyncio.run(runner.advance(task))
    with sessions() as session:
        row = session.get(BotDrawingTaskRow, task)
        assert row.generation_status == "cancelling" and row.cleanup_status != "done"


@pytest.mark.parametrize("status", ["queued", "submitting"])
def test_unsubmitted_task_requeues_after_restart_using_its_frozen_endpoint(sessions, status):
    import asyncio
    from al1s.bot_drawing.runner import DrawingTaskRunner

    service, _ = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    task = repo.admit(service, request, "456", "local",
        repo.prepare_route(service, request, PromptIntent(mode="direct", positive="cat"), "txt2img"),
        positive="cat", negative="", seed=1)
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.boot_id, row.execution_hash = status, "old-boot", "a" * 64
        row.resource_registration_state = "unknown" if status == "queued" else "confirmed"
        settings = session.get(BotDrawingSettingsRow, service)
        settings.row_version = settings.applied_version = 2
    seen = []
    class Resource:
        async def state(self): return {"boot_id": "new-boot"}
        async def close(self): pass
    class Configuration:
        async def vision_ready(self, _): return True
        async def client(self, _, **kwargs): seen.append(kwargs.get("endpoint")); return Resource()
    runner = DrawingTaskRunner(repo, Configuration(), None, None, None, lambda _: None)
    result = asyncio.run(runner.register(task) if status == "queued" else runner.advance(task))
    assert result == "queued" and seen == ["http://host.docker.internal:8188"]
    with sessions() as session:
        row = session.get(BotDrawingTaskRow, task)
        assert row.generation_status == "queued" and row.resource_registration_state == "pending"
        assert row.boot_id is None and not row.prompt_transport_started


def test_qq_history_keeps_output_until_manual_deletion_and_checks_current_audit(sessions, tmp_path):
    from io import BytesIO
    from PIL import Image
    from al1s.bot_drawing.media import DrawingMediaStore
    from al1s.bot_drawing.moderation import MODEL_REVISION, POLICY_VERSION

    service, _ = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    task = repo.admit(service, request, "456", "local",
        repo.prepare_route(service, request, PromptIntent(mode="direct", positive="cat"), "txt2img"),
        positive="cat", negative="blur", seed=1)
    buffer = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, format="PNG")
    body = buffer.getvalue()
    media = DrawingMediaStore(tmp_path)
    reference, checksum = media.put(task, "output", body)
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.cleanup_status, row.send_status = "succeeded", "done", "sent"
        row.output_reference, row.output_sha256, row.output_bytes = reference, checksum, len(body)
        row.audit_status, row.audit_sha256 = "allow", checksum
        row.audit_model_revision, row.audit_policy_version = MODEL_REVISION, POLICY_VERSION
        row.media_expires_at = datetime.now(UTC) - timedelta(days=2)
        row.created_at = datetime.now(UTC) - timedelta(days=40)
        session.get(BotDrawingSettingsRow, service).media_bytes = len(body)
    assert repo.preview_media(service, task) == (reference, checksum)
    assert media.read(reference, expected_sha256=checksum) == body
    assert repo.next_media_cleanup() is None and repo.purge_records() == 0
    assert repo.list_tasks(service, view="history")["items"][0]["has_image"]
    assert repo.get_task(service, task)["positive"] == "cat"
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.send_status, row.result_transport_started, row.reply_kind = "failed", False, "image"
    assert repo.retry_send(service, task, operation_id="retry-retained-image")
    with sessions.begin() as session:
        session.get(BotDrawingTaskRow, task).audit_status = "block"
    with pytest.raises(ValueError, match="drawing_media_unavailable"):
        repo.preview_media(service, task)


def test_full_retained_media_rejects_new_draws_and_reports_inflight_capacity_failure(sessions, tmp_path):
    import asyncio
    from io import BytesIO
    from PIL import Image
    from al1s.adapters.postgres.bot_drawing_repository import MAX_MEDIA_BYTES
    from al1s.bot_drawing.media import DrawingMediaStore
    from al1s.bot_drawing.runner import DrawingTaskRunner

    service, _ = ready_for_draw(sessions)
    repo, first = BotDrawingRepository(sessions), source()
    intent = PromptIntent(mode="direct", positive="cat")
    task = repo.admit(service, first, "456", "local", repo.prepare_route(service, first, intent, "txt2img"),
                      positive="cat", negative="", seed=1)
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.resource_registration_state = "running", "confirmed"
        row.boot_id, row.execution_hash, row.prompt_transport_started = "boot", "a" * 64, True
        session.get(BotDrawingSettingsRow, service).media_bytes = MAX_MEDIA_BYTES
    second = first.model_copy(update={"message_id": "20555"})
    with pytest.raises(ValueError, match="drawing_media_full"):
        repo.admit(service, second, "456", "local", repo.prepare_route(service, second, intent, "txt2img"),
                   positive="cat", negative="", seed=2)
    buffer = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, format="PNG")
    class Resource:
        async def download(self, *_): return buffer.getvalue()
        async def release(self, *_): return "retained"
        async def delete_history(self, *_): return True
    runner = DrawingTaskRunner(repo, None, DrawingMediaStore(tmp_path), None, None, lambda _: None)
    asyncio.run(runner.complete(task, {'status': 'completed'}, Resource(), output_node='9'))
    failed = repo.task_for_worker(task)
    assert failed['generation_status'] == 'failed' and failed['error_code'] == 'drawing_media_full'
    assert failed['cleanup_status'] == 'done' and failed['reply_kind'] == 'failure_notice'
    assert failed['output_reference'] is None


def test_batch_delete_cleans_only_selected_media_and_preserves_unknown_receipt(sessions, tmp_path):
    import asyncio
    from io import BytesIO
    from PIL import Image
    from al1s.bot_drawing.media import DrawingMediaStore
    from al1s.bot_drawing.runner import DrawingTaskRunner

    service, _ = ready_for_draw(sessions)
    repo, media = BotDrawingRepository(sessions), DrawingMediaStore(tmp_path)
    requests = [source().model_copy(update={"message_id": value}) for value in ("2001", "2002")]
    tasks = [repo.admit(service, request, "456", "local",
        repo.prepare_route(service, request, PromptIntent(mode="direct", positive="cat"), "txt2img"),
        positive="cat", negative="", seed=1) for request in requests]
    buffer = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, format="PNG")
    body = buffer.getvalue()
    files = [media.put(task, "output", body) for task in tasks]
    with sessions.begin() as session:
        for task, (reference, checksum) in zip(tasks, files):
            row = session.get(BotDrawingTaskRow, task)
            row.generation_status, row.cleanup_status, row.send_status = "succeeded", "done", "unknown"
            row.result_transport_started = True
            row.output_reference, row.output_sha256, row.output_bytes = reference, checksum, len(body)
        session.get(BotDrawingSettingsRow, service).media_bytes = len(body) * 2

    class Media:
        fail = True
        def staging(self): return media.staging()
        def delete_if_present(self, *args, **kwargs):
            if self.fail:
                self.fail = False
                raise OSError("synthetic temporary failure")
            return media.delete_if_present(*args, **kwargs)
        def remove_empty(self, task_id): return media.remove_empty(task_id)

    runner = DrawingTaskRunner(repo, None, Media(), None, None, lambda _: None)
    ids = repo.begin_delete_tasks(service, [tasks[0]], operation_id="delete-one")
    assert asyncio.run(runner.delete_history(ids)) == {"deleted": [], "pending": [str(tasks[0])]}
    with sessions() as session:
        assert session.get(BotDrawingTaskRow, tasks[0]).deletion_state == "deleting"
    assert asyncio.run(runner.delete_history(ids)) == {"deleted": [str(tasks[0])], "pending": []}
    assert repo.begin_delete_tasks(service, [tasks[0]], operation_id="delete-one") == ids
    with sessions() as session:
        deleted = session.get(BotDrawingTaskRow, tasks[0])
        assert deleted.deletion_state == "deleted" and deleted.send_status == "unknown"
        assert deleted.result_transport_started and deleted.positive is None and deleted.output_reference is None
        assert session.get(BotDrawingSettingsRow, service).media_bytes == len(body)
    assert repo.find_source(service, requests[0], "456") == tasks[0]
    assert media.read(files[1][0], expected_sha256=files[1][1]) == body
    with pytest.raises(ValueError, match="drawing_task_not_found"):
        repo.get_task(service, tasks[0])


def test_batch_delete_rejects_active_task_without_partially_marking_history(sessions):
    service, _ = ready_for_draw(sessions)
    repo, intent = BotDrawingRepository(sessions), PromptIntent(mode="direct", positive="cat")
    requests = [source().model_copy(update={"message_id": value}) for value in ("3001", "3002")]
    tasks = [repo.admit(service, request, "456", "local", repo.prepare_route(service, request, intent, "txt2img"),
                        positive="cat", negative="", seed=1) for request in requests]
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, tasks[1])
        row.generation_status, row.cleanup_status = "failed", "done"
    with pytest.raises(ValueError, match="drawing_task_busy"):
        repo.begin_delete_tasks(service, tasks, operation_id="delete-both")
    with sessions() as session:
        assert all(session.get(BotDrawingTaskRow, task).deletion_state == "visible" for task in tasks)


def test_cancel_during_input_audit_waits_until_auditor_releases_the_task(sessions):
    import asyncio
    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_drawing.runner import DrawingTaskRunner

    service, _ = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    task = repo.admit(service, request, "456", "local",
        repo.prepare_route(service, request, PromptIntent(mode="direct", positive="cat"), "txt2img"),
        positive="cat", negative="", seed=1)
    with sessions.begin() as session:
        session.get(BotDrawingTaskRow, task).processing_stage = "input_audit"

    class AuditFuture:
        finished = False
        def done(self): return self.finished
    auditor = type("Auditor", (), {"_pending": AuditFuture()})()

    async def run():
        queue = GpuQueue()
        queue.reserve(str(task))
        runner = DrawingTaskRunner(repo, None, None, auditor, None, lambda _: None, gpu_queue=queue)
        assert await asyncio.to_thread(repo.request_cancel, service, task, operation_id="cancel") == "cancelled"
        await runner.cancelled(task)
        await runner.recover_cleanup(task)
        assert queue.active == str(task)
        row = await asyncio.to_thread(repo.task_for_worker, task)
        assert row["processing_stage"] == "waiting_audit_stop" and row["send_status"] == "none"
        auditor._pending.finished = True
        await runner.recover_cleanup(task)
        row = await asyncio.to_thread(repo.task_for_worker, task)
        assert row["cleanup_status"] == "done" and row["send_status"] == "ready" and queue.idle
    asyncio.run(run())


def test_cancel_during_output_audit_waits_and_never_sends_image(sessions, tmp_path):
    import asyncio
    import hashlib
    from io import BytesIO
    from PIL import Image
    from al1s.bot_drawing.media import DrawingMediaStore
    from al1s.bot_drawing.moderation import AuditDecision
    from al1s.bot_drawing.runner import DrawingTaskRunner

    service, _ = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    task = repo.admit(service, request, "456", "local",
        repo.prepare_route(service, request, PromptIntent(mode="direct", positive="cat"), "txt2img"),
        positive="cat", negative="", seed=1)
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.resource_registration_state = "running", "confirmed"
        row.boot_id, row.execution_hash, row.prompt_transport_started = "boot", "a" * 64, True
    buffer = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, format="PNG")
    body = buffer.getvalue()

    async def run():
        entered, resume = asyncio.Event(), asyncio.Event()
        class Auditor:
            _pending = None
            async def audit(self, value):
                self._pending = asyncio.get_running_loop().create_future()
                entered.set()
                await resume.wait()
                self._pending.set_result(None)
                return AuditDecision("allow", hashlib.sha256(value).hexdigest())
        class Resource:
            owner = "drawing-owner"
            async def download(self, *_): return body
            async def release(self, *_): return "retained"
            async def delete_history(self, *_): return True
            async def state(self): return {"boot_id": "boot"}
            async def close(self): pass
        class Configuration:
            async def client(self, _, **_kwargs): return Resource()
        runner = DrawingTaskRunner(repo, Configuration(), DrawingMediaStore(tmp_path), Auditor(), None, lambda _: None)
        running = asyncio.create_task(runner.complete(task, {'status': 'completed'}, Resource(), output_node='9'))
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert repo.request_cancel(service, task, operation_id='cancel-output-audit') == 'cancelled'
        waiting = repo.task_for_worker(task)
        assert waiting['processing_stage'] == 'waiting_audit_stop'
        assert waiting['cleanup_status'] == 'deferred' and waiting['send_status'] == 'none'
        assert waiting['reply_kind'] is None and waiting['cancel_requested']
        await runner.recover_cleanup(task)
        assert repo.task_for_worker(task)['cleanup_status'] == 'deferred'
        resume.set()
        await asyncio.wait_for(running, timeout=2)
        await runner.recover_cleanup(task)
        finished = repo.task_for_worker(task)
        assert finished['cleanup_status'] == 'done' and finished['reply_kind'] == 'failure_notice'
        assert finished['send_status'] == 'ready' and not finished['result_transport_started']
    asyncio.run(run())


def test_audit_decision_is_bound_to_the_persisted_image_hash(sessions):
    from al1s.bot_drawing.moderation import AuditDecision
    service, _ = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    intent = PromptIntent(mode="direct", positive="cat")
    task = repo.admit(service, request, "456", "local", repo.prepare_route(service, request, intent, "txt2img"),
                      positive="cat", negative="", seed=1)
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.output_sha256 = "succeeded", "a" * 64
        version = row.row_version
    with pytest.raises(ValueError, match="drawing_audit_image_changed"):
        repo.record_audit(task, expected_version=version, decision=AuditDecision("allow", "b" * 64))
    repo.record_audit(task, expected_version=version, decision=AuditDecision("allow", "a" * 64))
    with sessions() as session:
        row = session.get(BotDrawingTaskRow, task)
        assert row.audit_status == "allow" and row.audit_sha256 == "a" * 64


def test_task_list_is_bounded_and_does_not_return_graphs_or_media_paths(sessions):
    with sessions.begin() as session:
        service = add_service(session, "one")
        preset = add_preset(session, service)
        for index in range(3):
            task = make_task(service, preset, str(index + 1), str(100 + index))
            task.created_at = datetime.now(UTC) + timedelta(seconds=index)
            session.add(task)
    repo = BotDrawingRepository(sessions)
    first = repo.list_tasks(service, limit=2)
    second = repo.list_tasks(service, limit=2, after_id=UUID(first["next_after_id"]))
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert {item["id"] for item in first["items"]}.isdisjoint(item["id"] for item in second["items"])
    assert all("input_reference" not in item and "positive" not in item for item in first["items"])


def test_preview_has_no_qq_identity_and_only_verified_output_enables_preset(sessions):
    from al1s.bot_drawing.moderation import MODEL_REVISION, POLICY_VERSION
    with sessions.begin() as session:
        service = add_service(session, "one")
    repo = BotDrawingRepository(sessions)
    preset = repo.save_preset(service, "角色", frozen_workflow(), {}, enabled=True,
                              preset_id=None, expected_version=0, operation_id="new")
    dependencies = {"adapter": "test", "boot_id": "boot-1", "nodes": [
        {"name": "CLIPTextEncode", "sha256": "f" * 64}, {"name": "SaveImage", "sha256": "f" * 64}],
        "assets": [{"kind": "loras", "name": "character.safetensors", "size": 123, "sha256": "e" * 64}]}
    repo.record_check(service, preset.id, preset.revision, preset.row_version, dependencies, operation_id="check")
    task_id = uuid4()
    args = dict(task_id=task_id, positive="cat", negative="", seed=1, mode="green", operation_id="preview",
                expected_version=preset.row_version)
    assert repo.admit_preview(service, preset.id, "local", **args) == task_id
    assert repo.admit_preview(service, preset.id, "local", **args) == task_id
    with sessions() as session:
        task = session.get(BotDrawingTaskRow, task_id)
        assert task.origin == "admin_preview" and task.scope_kind is None and task.sender_id is None
    with pytest.raises(ValueError, match="drawing_preview_unverified"):
        repo.mark_preview_validated(task_id, measured_peak_bytes=100)
    with sessions.begin() as session:
        task = session.get(BotDrawingTaskRow, task_id)
        task.generation_status, task.cleanup_status = "succeeded", "done"
        task.resource_registration_state, task.prompt_transport_started = "confirmed", True
        task.output_sha256, task.output_reference, task.output_bytes = "a" * 64, f"al1s-drawing/{task_id}/output.png", 100
        task.audit_status, task.audit_sha256 = "allow", "a" * 64
        task.audit_model_revision, task.audit_policy_version = MODEL_REVISION, POLICY_VERSION
    repo.mark_preview_validated(task_id, measured_peak_bytes=100)
    with sessions() as session:
        assert session.get(BotDrawingRevisionRow, (preset.id, 1)).validation_state == "ready"


def test_workflow_command_reply_is_claimed_once_and_unknown_is_not_resent(sessions):
    service, _ = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    key, receipt = repo.claim_control(service, request, "456", "/wf", "工作流列表")
    assert receipt["status"] == "claimed" and repo.start_control(service, key, request, 1)
    repo.finish_control(service, key, "unknown")
    replay_key, replay = repo.claim_control(service, request, "456", "/wf", "新的列表")
    assert replay_key == key and replay["status"] == "unknown" and replay["text"] == "工作流列表"
    assert not repo.start_control(service, key, request, 1)


def test_replayed_workflow_command_cannot_revert_a_newer_selection(sessions):
    service, _ = ready_for_draw(sessions)
    with sessions.begin() as session:
        second = add_preset(session, service, number=2)
    repo, request = BotDrawingRepository(sessions), source()
    repo.claim_control(service, request, "456", "/wf1", "已选择 #1", workflow_number=1)
    repo.set_selection(service, request, 2)
    repo.claim_control(service, request, "456", "/wf1", "已选择 #1", workflow_number=1)
    with sessions() as session:
        assert session.get(BotDrawingSelectionRow, (service, "qq_group", "123", "1")).preset_id == second


def test_queued_cancel_is_idempotent_and_unknown_delivery_cannot_retry(sessions):
    service, _ = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    intent = PromptIntent(mode="direct", positive="cat")
    task = repo.admit(service, request, "456", "local", repo.prepare_route(service, request, intent, "txt2img"),
                      positive="cat", negative="", seed=1)
    assert repo.request_cancel(service, task, operation_id="cancel") == "cancelled"
    assert repo.request_cancel(service, task, operation_id="cancel") == "cancelled"
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.send_status, row.result_transport_started = "unknown", True
    with pytest.raises(ValueError, match="drawing_send_retry_invalid"):
        repo.retry_send(service, task, operation_id="retry")


def test_media_quota_is_reserved_once_and_cannot_exceed_one_gib(sessions):
    service, _ = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    intent = PromptIntent(mode="direct", positive="cat")
    task = repo.admit(service, request, "456", "local", repo.prepare_route(service, request, intent, "txt2img"),
                      positive="cat", negative="", seed=1)
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status = "running"
        session.get(BotDrawingSettingsRow, service).media_bytes = 1024 ** 3 - 100
        version = row.row_version
    with pytest.raises(ValueError, match="drawing_media_full"):
        repo.reserve_output(task, expected_version=version, size=101, checksum="a" * 64)
    version = repo.reserve_output(task, expected_version=version, size=100, checksum="a" * 64)
    assert repo.reserve_output(task, expected_version=version, size=100, checksum="a" * 64) == version
    with sessions() as session:
        assert session.get(BotDrawingSettingsRow, service).media_bytes == 1024 ** 3


def test_media_accounting_releases_only_expired_owned_reservation(sessions):
    service, _ = ready_for_draw(sessions)
    repo, request = BotDrawingRepository(sessions), source()
    intent = PromptIntent(mode="direct", positive="cat")
    task = repo.admit(service, request, "456", "local", repo.prepare_route(service, request, intent, "txt2img"),
                      positive="cat", negative="", seed=1)
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status = "running"
        version = row.row_version
    repo.reserve_output(task, expected_version=version, size=100, checksum="a" * 64)
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.cleanup_status = "failed", "done"
        row.media_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not repo.clear_media(task, "output", checksum="b" * 64, size=100)
    assert repo.clear_media(task, "output", checksum="a" * 64, size=100)
    assert not repo.clear_media(task, "output", checksum="a" * 64, size=100)
    with sessions() as session:
        assert session.get(BotDrawingSettingsRow, service).media_bytes == 0


def test_route_admission_and_claim_use_bounded_database_round_trips(sessions):
    service, _ = ready_for_draw(sessions)
    repo, request, statements = BotDrawingRepository(sessions), source(), []
    engine = sessions.kw["bind"]
    def record(_connection, _cursor, statement, _parameters, _context, _many): statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        route = repo.prepare_route(service, request, PromptIntent(mode="direct", positive="cat"), "txt2img",
                                   workflow_number=1)
        assert len([value for value in statements if value.lstrip().upper().startswith("SELECT")]) <= 3
        statements.clear()
        task = repo.admit(service, request, "456", "local", route, positive="cat", negative="", seed=1)
        assert len(statements) <= 12
        assert repo.queued_count('local') == 1 and repo.queued_count('other') == 0
        repo.mark_registration(task, expected_version=1, boot_id="boot-1", execution_hash="a" * 64)
        statements.clear()
        assert repo.claim_next("local").id == task
        assert len(statements) <= 4
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_preview_pipeline_persists_audits_releases_and_enables_revision(sessions, tmp_path):
    import asyncio
    import hashlib
    from io import BytesIO
    from PIL import Image
    from al1s.bot_drawing.media import DrawingMediaStore
    from al1s.bot_drawing.moderation import AuditDecision
    from al1s.bot_drawing.runner import DrawingTaskRunner
    from al1s.bot_ai.gpu_queue import GpuQueue

    async def run():
        with sessions.begin() as session:
            service = add_service(session, "one")
        repo = BotDrawingRepository(sessions)
        preset = repo.save_preset(service, "角色", frozen_workflow(with_negative=True), {}, enabled=True,
                                  preset_id=None, expected_version=0, operation_id="new")
        nodes = [{"name": "CLIPTextEncode", "sha256": "f" * 64}, {"name": "SaveImage", "sha256": "f" * 64}]
        assets = [{"kind": "loras", "name": "character.safetensors", "size": 123, "sha256": "e" * 64}]
        deps = {"adapter": "test", "boot_id": "boot-1", "nodes": nodes, "assets": assets}
        repo.record_check(service, preset.id, 1, 1, deps, operation_id="check")
        task_id = uuid4()
        repo.admit_preview(service, preset.id, "comfy-local-8188", task_id=task_id, positive="cat", negative="",
                           seed=1, mode="green", expected_version=1, operation_id="preview")
        queue = GpuQueue()
        queue.reserve(str(task_id))
        image = BytesIO(); Image.new("RGB", (8, 8), "blue").save(image, "PNG"); body = image.getvalue()
        class Resource:
            owner = "test-owner"
            async def state(self): return {"adapter": "test", "boot_id": "boot-1", "peak_allocated_bytes": 123}
            async def inspect(self, *_): return deps
            async def register(self, *_): return {"state": "registered"}
            async def switch(self, *_): return "ready"
            async def submit(self, *_): return "accepted"
            async def query(self, prompt, *_): return {"id": str(prompt), "status": "completed"}
            async def download(self, *_): return body
            async def release(self, *_): return "retained"
            async def delete_history(self, *_): return True
            async def close(self): pass
        class Config:
            async def vision_ready(self, _): return True
            async def client(self, _, **_kwargs): return Resource()
        class Auditor:
            async def audit(self, data):
                assert repo.task_for_worker(task_id)["cleanup_status"] == "pending"
                assert queue.idle
                return AuditDecision("allow", hashlib.sha256(data).hexdigest())
        media = DrawingMediaStore(tmp_path)
        runner = DrawingTaskRunner(repo, Config(), media, Auditor(), None, lambda _: None, gpu_queue=queue)
        assert await runner.register(task_id) == "confirmed"
        assert repo.claim_next("comfy-local-8188").id == task_id
        assert await runner.advance(task_id) == "running"
        assert await runner.advance(task_id) == "completed"
        assert repo.get_preset(service, preset.id)["validation_state"] == "ready"
        reference, checksum = repo.preview_media(service, task_id)
        assert media.read(reference, expected_sha256=checksum) == body
    asyncio.run(run())


def test_saved_output_with_pending_cpu_audit_is_recovered_after_restart(sessions, tmp_path):
    import asyncio
    import hashlib
    from io import BytesIO
    from PIL import Image
    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_drawing.media import DrawingMediaStore
    from al1s.bot_drawing.moderation import AuditDecision
    from al1s.bot_drawing.runner import DrawingTaskRunner

    async def run():
        task_id, media = uuid4(), DrawingMediaStore(tmp_path)
        image = BytesIO()
        Image.new("RGB", (8, 8), "blue").save(image, "PNG")
        body = image.getvalue()
        reference, checksum = media.put(task_id, "output", body)
        with sessions.begin() as session:
            service = add_service(session, "audit-recovery")
            preset = add_preset(session, service)
            session.add(BotDrawingTaskRow(id=task_id, service_id=service, operation="draw", origin="admin_preview",
                preset_id=preset, preset_revision=1, dependency_signature="a" * 64,
                endpoint_key="test", prompt_id=uuid4(), created_at=datetime.now(UTC),
                generation_status="succeeded", cleanup_status="pending", audit_mode="green",
                resource_registration_state="pending", prompt_transport_started=False,
                output_reference=reference, output_sha256=checksum, output_bytes=len(body)))
        repo, queue = BotDrawingRepository(sessions), GpuQueue()
        queue.reserve(str(task_id))
        class Auditor:
            async def audit(self, data): return AuditDecision("allow", hashlib.sha256(data).hexdigest())
        runner = DrawingTaskRunner(repo, None, media, Auditor(), None, None, gpu_queue=queue)
        await runner.recover_cleanup(task_id)
        result = repo.task_for_worker(task_id)
        assert result["audit_status"] == "allow" and result["reply_kind"] == "image"
        assert result["cleanup_status"] == "done" and queue.idle
    asyncio.run(run())


def test_expired_unregistered_media_is_removed_without_touching_owned_or_unknown_files(sessions, tmp_path):
    import os
    import time
    from io import BytesIO
    from PIL import Image
    from al1s.bot_drawing.media import DrawingMediaStore

    service, _ = ready_for_draw(sessions)
    repo, request, media = BotDrawingRepository(sessions), source(), DrawingMediaStore(tmp_path)
    route = repo.prepare_route(service, request, PromptIntent(mode='direct', positive='cat'), 'txt2img')
    existing = repo.admit(service, request, '456', 'local', route, positive='cat', negative='', seed=1)
    orphan, recent, unknown = uuid4(), uuid4(), uuid4()
    output = BytesIO(); Image.new('RGB', (8, 8), 'blue').save(output, 'PNG'); image = output.getvalue()
    for task in (existing, orphan, recent):
        media.put(task, 'input', image)
    root = tmp_path / 'al1s-drawing'
    unknown_dir = root / str(unknown); unknown_dir.mkdir(); (unknown_dir / 'user.txt').write_text('keep')
    old = time.time() - 7200
    for task in (existing, orphan, unknown):
        directory = root / str(task)
        for item in directory.iterdir(): os.utime(item, (old, old))
        os.utime(directory, (old, old))
    assert repo.cleanup_orphan_media(media) == 1
    assert not (root / str(orphan)).exists()
    assert (root / str(existing) / 'input.png').exists()
    assert (root / str(recent) / 'input.png').exists()
    assert (unknown_dir / 'user.txt').read_text() == 'keep'


def test_queued_task_expires_while_another_task_is_running_and_preserves_unknown_registration(sessions):
    service, _ = ready_for_draw(sessions)
    repo = BotDrawingRepository(sessions)
    first = source()
    second = CommandSource(message_id='999', scope_kind='qq_group', target_id='123', sender_id='987')
    intent = PromptIntent(mode='direct', positive='cat')
    first_id = repo.admit(service, first, '456', 'local', repo.prepare_route(service, first, intent, 'txt2img'),
                          positive='cat', negative='', seed=1)
    second_id = repo.admit(service, second, '456', 'local', repo.prepare_route(service, second, intent, 'txt2img'),
                           positive='cat', negative='', seed=1)
    repo.registration_unknown(second_id, expected_version=1, boot_id='boot-1', execution_hash='a' * 64)
    with sessions.begin() as session:
        session.get(BotDrawingTaskRow, first_id).generation_status = 'running'
        session.get(BotDrawingTaskRow, second_id).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert repo.expire_next('local') == second_id
    with sessions() as session:
        expired = session.get(BotDrawingTaskRow, second_id)
        assert expired.generation_status == 'expired' and expired.cleanup_status == 'deferred'
        assert expired.send_status == 'ready' and expired.reply_kind == 'failure_notice'
        assert expired.error_code == 'drawing_queue_expired' and expired.finished_at is not None
