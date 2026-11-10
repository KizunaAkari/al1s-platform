import asyncio
import base64
import hashlib
import json
from io import BytesIO
from uuid import UUID, uuid4

import httpx
import pytest
from PIL import Image

from al1s.adapters.postgres.bot_drawing_models import BotDrawingGroupModeRow, BotDrawingSettingsRow, BotDrawingTaskRow
from al1s.adapters.postgres.bot_drawing_repository import BotDrawingRepository
from al1s.api.bot_drawing import OnlinePresetWrite, PromptProviderWrite
from al1s.bot_drawing.contracts import PromptIntent
from al1s.bot_drawing.media import DrawingMediaStore
from al1s.bot_drawing.moderation import AuditDecision
from al1s.bot_drawing.runner import DrawingTaskRunner
from al1s.meme_workshop.contracts import CommandSource
from al1s.secrets.security import FernetSecretCipher
from tests.integration.test_bot_drawing_persistence import ready_for_draw, sessions

pytestmark = pytest.mark.integration


def source(message):
    return CommandSource(message_id=message, scope_kind="qq_group", target_id="123", sender_id="1")


def image_bytes():
    result = BytesIO()
    Image.new("RGB", (8, 8), "red").save(result, format="PNG")
    return result.getvalue()


def online_setup(sessions):
    service, local = ready_for_draw(sessions)
    with sessions.begin() as session:
        session.get(BotDrawingSettingsRow, service).next_workflow_number = 2
    repo = BotDrawingRepository(sessions, cipher=FernetSecretCipher("online-test-key-with-at-least-32-characters"))
    data = OnlinePresetWrite(name="线上绘图", model_id="gpt-image-2.6-flare", enabled=True,
                             size="1024x1024", quality="medium", secret_input="example-only-test-key")
    preset = repo.save_online_preset(service, data, preset_id=None, expected_version=0, operation_id="create")
    settings = repo.save_settings(service, enabled=True, keep_drawing_warm=False,
        default_text_preset_id=local, default_edit_preset_id=None, default_online_preset_id=UUID(preset["id"]),
        expected_version=1, operation_id="default")
    assert repo.mark_applied(service, settings.row_version)
    return service, local, preset, repo


def admit(repo, service, message, **kwargs):
    task = uuid4()
    return repo.admit_online(service, task_id=task, source=source(message), bot_self_id="456",
                             positive="画一只猫", **kwargs)


def test_online_preset_is_versioned_and_works_while_local_resources_are_unsynced(sessions):
    service, local, preset, repo = online_setup(sessions)
    task = admit(repo, service, "100")
    detail = repo.get_preset(service, UUID(preset["id"]))
    assert detail["secret_configured"] and "secret_input" not in detail and "online_secret_id" not in detail
    updated = OnlinePresetWrite(name="线上绘图", model_id="gpt-image-2.6-sunburst", enabled=True)
    repo.save_online_preset(service, updated, preset_id=UUID(preset["id"]), expected_version=1, operation_id="edit")
    later = admit(repo, service, "101", preset_number=preset["number"])
    assert repo.task_for_worker(task)["preset_revision"] == 1
    assert repo.task_for_worker(later)["preset_revision"] == 2
    assert repo.online_credentials(service, UUID(preset["id"]), 1)[0]["model_id"] == "gpt-image-2.6-flare"
    assert repo.online_credentials(service, UUID(preset["id"]), 2)[0]["model_id"] == "gpt-image-2.6-sunburst"
    assert repo.get_settings(service)["resource_applied_version"] == 1
    with pytest.raises(ValueError, match="drawing_resources_unavailable"):
        repo.prepare_route(service, source("102"), PromptIntent(mode="direct", positive="cat"), "txt2img")


def test_auto_defaults_preserve_existing_preset_and_queued_revision(sessions):
    service, _, preset, repo = online_setup(sessions)
    identity = UUID(preset['id'])
    fixed = OnlinePresetWrite(name='线上绘图', model_id='gpt-image-2.6-flare', enabled=True,
                              size='1024x1536', quality='high')
    repo.save_online_preset(service, fixed, preset_id=identity, expected_version=1, operation_id='fixed')
    original = repo.get_preset(service, identity)
    assert original['size'] == '1024x1536' and original['quality'] == 'high'
    queued = admit(repo, service, '901')
    create = OnlinePresetWrite(name='自动预设', model_id='gpt-image-2.6-flare',
                               secret_input='synthetic-auto-key')
    added = repo.save_online_preset(service, create, preset_id=None, expected_version=0, operation_id='auto-create')
    detail = repo.get_preset(service, UUID(added['id']))
    assert detail['size'] == detail['quality'] == 'auto'
    edit = OnlinePresetWrite(name='线上绘图改名', model_id='gpt-image-2.6-flare', enabled=True)
    changed = repo.save_online_preset(service, edit, preset_id=identity, expected_version=2, operation_id='rename')
    detail = repo.get_preset(service, UUID(changed['id']))
    assert detail['size'] == '1024x1536' and detail['quality'] == 'high'
    assert repo.task_for_worker(queued)['preset_revision'] == 2
    assert repo.online_credentials(service, identity, 2)[0]['size'] == '1024x1536'
    select_auto = OnlinePresetWrite(name='线上绘图改名', model_id='gpt-image-2.6-flare', enabled=True,
                                    size='auto', quality='auto')
    repo.save_online_preset(service, select_auto, preset_id=identity, expected_version=3, operation_id='select-auto')
    assert repo.get_preset(service, identity)['size'] == 'auto'
    assert repo.online_credentials(service, identity, 2)[0]['quality'] == 'high'


def test_legacy_omitted_defaults_replay_the_existing_preset(sessions):
    from al1s.adapters.postgres.meme_base import fingerprint
    from al1s.adapters.postgres.meme_models import MemeMutationRow
    service, _, _, repo = online_setup(sessions)
    old = OnlinePresetWrite(name='旧客户端预设', model_id='gpt-image-2.6-flare',
                            size='1024x1024', quality='medium', secret_input='synthetic-legacy-key')
    first = repo.save_online_preset(service, old, preset_id=None, expected_version=0, operation_id='legacy-replay')
    serialized = old.model_dump(mode='json')
    serialized['secret_input'] = repo.cipher.fingerprint(old.secret_input.get_secret_value())
    digest = fingerprint({'preset_id': None, 'expected': 0, 'data': serialized})
    with sessions.begin() as session:
        session.get(MemeMutationRow, repo.mutation_key('drawing_online_preset', service, 'legacy-replay')).request_sha256 = digest
    retry = OnlinePresetWrite(name='旧客户端预设', model_id='gpt-image-2.6-flare',
                              secret_input='synthetic-legacy-key')
    replay = repo.save_online_preset(service, retry, preset_id=None, expected_version=0, operation_id='legacy-replay')
    assert replay == first
    detail = repo.get_preset(service, UUID(first['id']))
    assert detail['size'] == '1024x1024' and detail['quality'] == 'medium'


def test_new_create_rejects_reusing_key_with_different_auto_defaults(sessions):
    service, _, _, repo = online_setup(sessions)
    fixed = OnlinePresetWrite(name='新客户端预设', model_id='gpt-image-2.6-flare',
                              size='1024x1024', quality='medium', secret_input='synthetic-new-key')
    repo.save_online_preset(service, fixed, preset_id=None, expected_version=0, operation_id='changed-create')
    changed = OnlinePresetWrite(name='新客户端预设', model_id='gpt-image-2.6-flare', secret_input='synthetic-new-key')
    with pytest.raises(ValueError, match='idempotency_conflict'):
        repo.save_online_preset(service, changed, preset_id=None, expected_version=0, operation_id='changed-create')


def test_legacy_explicit_defaults_replay_without_recreating_preset(sessions):
    from al1s.adapters.postgres.meme_base import fingerprint
    from al1s.adapters.postgres.meme_models import MemeMutationRow
    service, _, _, repo = online_setup(sessions)
    old = OnlinePresetWrite(name='旧版固定预设', model_id='gpt-image-2.6-flare',
                            size='1024x1536', quality='high', secret_input='synthetic-explicit-key')
    first = repo.save_online_preset(service, old, preset_id=None, expected_version=0, operation_id='legacy-explicit')
    serialized = old.model_dump(mode='json')
    serialized['secret_input'] = repo.cipher.fingerprint(old.secret_input.get_secret_value())
    digest = fingerprint({'preset_id': None, 'expected': 0, 'data': serialized})
    with sessions.begin() as session:
        session.get(MemeMutationRow, repo.mutation_key('drawing_online_preset', service, 'legacy-explicit')).request_sha256 = digest
    assert repo.save_online_preset(service, old, preset_id=None, expected_version=0,
                                   operation_id='legacy-explicit') == first


def test_omitted_update_replays_but_explicit_auto_is_not_the_same_request(sessions):
    service, _, preset, repo = online_setup(sessions)
    identity = UUID(preset['id'])
    unchanged = OnlinePresetWrite(name='线上绘图', model_id='gpt-image-2.6-flare', enabled=True)
    first = repo.save_online_preset(service, unchanged, preset_id=identity, expected_version=1, operation_id='partial')
    assert repo.save_online_preset(service, unchanged, preset_id=identity,
                                   expected_version=1, operation_id='partial') == first
    explicit = OnlinePresetWrite(name='线上绘图', model_id='gpt-image-2.6-flare', enabled=True,
                                 size='auto', quality='auto')
    with pytest.raises(ValueError, match='idempotency_conflict'):
        repo.save_online_preset(service, explicit, preset_id=identity, expected_version=1, operation_id='partial')


def test_legacy_settings_write_preserves_online_default(sessions):
    service, local, preset, repo = online_setup(sessions)
    settings = repo.save_settings(service, enabled=True, keep_drawing_warm=False,
        default_text_preset_id=local, default_edit_preset_id=None, expected_version=2, operation_id="legacy-save")
    assert settings.default_online_preset_id == UUID(preset["id"])
    assert repo.get_settings(service)["default_online_preset_id"] == preset["id"]


def test_group_restriction_and_cancel_after_submission_do_not_replay(sessions):
    service, _, preset, repo = online_setup(sessions)
    with sessions.begin() as session:
        session.add(BotDrawingGroupModeRow(service_id=service, group_id="123", mode="green", restrict_workflows=True))
    with pytest.raises(ValueError, match="drawing_group_preset_denied"):
        admit(repo, service, "110")
    with sessions.begin() as session:
        session.get(BotDrawingGroupModeRow, (service, "123")).restrict_workflows = False
    task = admit(repo, service, "111")
    next_task = admit(repo, service, "112")
    assert repo.claim_online_next() == task
    assert repo.online_state(task, "submitting") and repo.online_state(task, "sent")
    assert repo.request_cancel(service, task, operation_id="cancel") == "cancelled"
    assert not repo.online_state(task, "received")
    assert not repo.fail_online(task, "drawing_online_moderation_blocked", "failed",
                                {"stage": "output", "categories": ["violence"]})
    row = repo.task_for_worker(task)
    assert row["generation_status"] == "cancelled" and row["online_request_sent"]
    assert row["online_moderation_details"] is None
    assert not row["online_image_received"] and row["cleanup_status"] == "done"
    assert repo.claim_online_next() == next_task


def test_restart_keeps_unsent_queue_and_marks_inflight_result_unknown(sessions):
    service, _, _, repo = online_setup(sessions)
    first, second = admit(repo, service, "120"), admit(repo, service, "121")
    assert repo.claim_online_next() == first
    assert repo.online_state(first, "submitting") and repo.online_state(first, "sent")
    repo.recover_online()
    row = repo.task_for_worker(first)
    assert row["generation_status"] == "failed" and row["online_request_state"] == "unknown"
    assert row["online_request_sent"] and not row["online_image_received"]
    assert repo.claim_online_next() == second


@pytest.mark.parametrize('stage', ['cleaning', 'input_audit', 'output_audit', 'waiting_audit_stop'])
def test_active_tasks_include_finished_generation_still_in_audit_or_cleanup(sessions, stage):
    service, _, _, repo = online_setup(sessions)
    task = admit(repo, service, '118')
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.processing_stage = 'succeeded', stage
        row.send_status, row.cleanup_status = 'none', 'none'
    active = repo.list_tasks(service, view='active')['items']
    assert [item['id'] for item in active] == [str(task)]
    assert active[0]['processing_stage'] == stage


def test_task_flow_fields_are_bounded_and_do_not_expose_prompt_or_image_hash(sessions):
    service, _, _, repo = online_setup(sessions)
    task = admit(repo, service, "119")
    queued = repo.list_tasks(service, view="active")["items"][0]
    assert queued["prompt_model_used"] is False
    assert queued["has_input_image"] is False
    assert queued["prompt_status"] == "ready"
    assert queued["input_audit_status"] == "pending"
    assert queued["prompt_transport_started"] is False
    assert queued["has_started"] is False
    assert "wish_text" not in queued and "positive" not in queued and "input_sha256" not in queued
    assert repo.claim_online_next() == task
    assert repo.online_state(task, "submitting") and repo.online_state(task, "sent")
    assert repo.fail_online(task, "drawing_online_auth_failed", "failed")
    history = repo.list_tasks(service, view="history")["items"][0]
    assert history["has_input_image"] is None  # Input media may have been cleaned after completion.
    assert history["prompt_transport_started"] is True
    assert history["has_started"] is True
    assert history["reply_kind"] == "failure_notice"
    detail = repo.get_task(service, task)
    for key in ("prompt_model_used", "has_input_image", "prompt_status", "input_audit_status",
                "resource_registration_state", "prompt_transport_started", "has_started", "reply_kind"):
        assert detail[key] == history[key]


def test_online_queue_does_not_send_a_stale_notice_after_execution_starts(sessions):
    service, _, _, repo = online_setup(sessions)
    task = admit(repo, service, "125")
    assert repo.next_delivery("openai-images") == (task, "notice")
    assert repo.claim_online_next() == task
    assert repo.next_delivery("openai-images") is None


def test_optional_prompt_model_is_frozen_when_each_online_task_starts(sessions):
    service, _, preset, repo = online_setup(sessions)
    online = OnlinePresetWrite(name="线上绘图", model_id="gpt-image-2.6-flare", enabled=True, use_prompt_model=True)
    repo.save_online_preset(service, online, preset_id=UUID(preset["id"]), expected_version=1, operation_id="expand")
    models = []
    for name in ("第一套", "第二套"):
        data = PromptProviderWrite(name=name, adapter_kind="openai_responses", model_id="model-for-test",
                                   secret_input="example-prompt-key")
        models.append(repo.save_prompt_model(service, data, profile_id=None, expected_version=0, operation_id=name))
    first_model, second_model = (UUID(item["id"]) for item in models)
    repo.activate_prompt_model(service, first_model, expected_version=1, operation_id="select-first")
    first, second = admit(repo, service, "122"), admit(repo, service, "123")
    assert repo.claim_online_next() == first
    profile, revision = repo.begin_prompt_expansion(first)
    repo.activate_prompt_model(service, second_model, expected_version=2, operation_id="select-second")
    repo.finish_prompt_expansion(first, profile, revision, "first prompt", "")
    assert repo.task_for_worker(first)["prompt_profile_id"] == first_model
    assert repo.fail_online(first, "drawing_online_unknown", "failed")
    assert repo.claim_online_next() == second
    assert repo.begin_prompt_expansion(second)[0] == second_model


def test_online_wish_retains_original_marked_text_and_separate_negative(sessions):
    service, _, preset, repo = online_setup(sessions)
    online = OnlinePresetWrite(name="线上绘图", model_id="gpt-image-2.6-flare", enabled=True, use_prompt_model=True)
    repo.save_online_preset(service, online, preset_id=UUID(preset["id"]), expected_version=1, operation_id="expand-marked")
    task = admit(repo, service, "1234", negative="blur", original_positive="cat【NP】blur")
    row = repo.task_for_worker(task)
    assert row["wish_text"] == "cat【NP】blur" and row["negative"] == "blur"
    assert row["prompt_status"] == "waiting"


def test_group_revoking_online_preset_prevents_pending_image_delivery(sessions):
    service, _, _, repo = online_setup(sessions)
    task = admit(repo, service, "124")
    assert repo.claim_online_next() == task
    assert repo.online_state(task, "submitting") and repo.online_state(task, "sent")
    assert repo.online_state(task, "received")
    row = repo.task_for_worker(task)
    checksum = "a" * 64
    version = repo.reserve_output(task, expected_version=row["row_version"], size=5, checksum=checksum)
    version = repo.transition(task, "submitting", "succeeded", expected_version=version,
        output_reference=f"al1s-drawing/{task}/output.png", output_sha256=checksum, output_bytes=5)
    version = repo.record_audit(task, expected_version=version, decision=AuditDecision("allow", checksum))
    version = repo.set_result_ready(task, expected_version=version, reply_kind="image")
    repo.mark_cleanup(task, expected_version=version, status="done")
    assert repo.next_delivery("openai-images") == (task, "result")
    with sessions.begin() as session:
        session.add(BotDrawingGroupModeRow(service_id=service, group_id="123", mode="green", restrict_workflows=True))
    assert repo.next_delivery("openai-images") is None
    assert repo.start_send(task, "result")
    assert not repo.mark_transport_started(task, "result", checksum)


def test_mock_online_generation_saves_and_audits_image_without_comfy(sessions, tmp_path, monkeypatch):
    service, _, _, repo = online_setup(sessions)
    task = admit(repo, service, "130")
    assert repo.claim_online_next() == task
    original_client = httpx.AsyncClient
    seen = []
    async def provider(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image_bytes()).decode()}]})
    def mock_client(**kwargs):
        kwargs.pop("proxy", None)
        return original_client(transport=httpx.MockTransport(provider), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    class Auditor:
        async def audit(self, body):
            return AuditDecision("allow", hashlib.sha256(body).hexdigest())
    media = DrawingMediaStore(tmp_path)
    runner = DrawingTaskRunner(repo, None, media, Auditor(), None, lambda _: None)
    asyncio.run(runner._execute_online(task))
    row = repo.task_for_worker(task)
    assert seen[0]["model"] == "gpt-image-2.6-flare" and "SFW" in seen[0]["prompt"]
    assert seen[0]["size"] == "1024x1024" and seen[0]["output_format"] == "png"
    assert row["generation_status"] == "succeeded" and row["audit_status"] == "allow"
    assert row["online_request_sent"] and row["online_image_received"]
    assert row["reply_kind"] == "image" and row["cleanup_status"] == "done"
    assert media.read(row["output_reference"], expected_sha256=row["output_sha256"])


def test_mock_online_moderation_details_are_persisted_and_not_replaced_by_late_results(sessions, monkeypatch):
    service, _, _, repo = online_setup(sessions)
    task = admit(repo, service, "1301")
    assert repo.claim_online_next() == task
    original_client = httpx.AsyncClient
    async def provider(_request):
        return httpx.Response(400, json={"error": {"code": "moderation_blocked", "message": "private text",
            "moderation_details": {"moderation_stage": "input", "categories": ["sexual", "untrusted-secret"]}}})
    def mock_client(**kwargs):
        kwargs.pop("proxy", None)
        return original_client(transport=httpx.MockTransport(provider), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    runner = DrawingTaskRunner(repo, None, None, None, None, lambda _: None)
    asyncio.run(runner._execute_online(task))
    details = {"stage": "input", "categories": ["sexual", "unrecognized"]}
    assert repo.task_for_worker(task)["error_code"] == "drawing_online_moderation_blocked"
    assert repo.get_task(service, task)["online_moderation_details"] == details
    assert "online_moderation_details" not in repo.list_tasks(service, view="history")["items"][0]
    assert not repo.fail_online(task, "drawing_online_moderation_blocked", "failed", {"stage": "output"})
    assert repo.get_task(service, task)["online_moderation_details"] == details


@pytest.mark.parametrize('expand', [False, True])
@pytest.mark.parametrize('gif', [False, True])
def test_auto_settings_leave_user_resolution_and_quality_in_prompt(sessions, tmp_path, monkeypatch, expand, gif):
    service, _, preset, repo = online_setup(sessions)
    edited = OnlinePresetWrite(name='线上绘图', model_id='gpt-image-2.6-flare', enabled=True,
                               size='auto', quality='auto', use_prompt_model=expand)
    repo.save_online_preset(service, edited, preset_id=UUID(preset['id']), expected_version=1, operation_id='auto-settings')
    original = '画一只 2048x1024 高画质的猫' + (' GIF 动图' if gif else '')
    task = repo.admit_online(service, task_id=uuid4(), source=source('902'), bot_self_id='456',
                             positive=original, negative='水印', original_positive=original+'【NP】水印')
    assert repo.claim_online_next() == task
    picture = image_bytes()
    if gif:
        output = BytesIO()
        frames = [Image.new('RGB', (8, 8), color) for color in ('red', 'blue')]
        frames[0].save(output, 'GIF', save_all=True, append_images=frames[1:], duration=200, loop=0)
        picture = output.getvalue()
    original_client, seen = httpx.AsyncClient, []
    async def provider(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(picture).decode()}]})
    def mock_client(**kwargs):
        kwargs.pop('proxy', None)
        return original_client(transport=httpx.MockTransport(provider), **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', mock_client)
    class Auditor:
        async def audit(self, body): return AuditDecision('allow', hashlib.sha256(body).hexdigest())
    runner = DrawingTaskRunner(repo, None, DrawingMediaStore(tmp_path), Auditor(), None, lambda _: None,
                               prompt_model=object() if expand else None)
    if expand:
        async def expand_prompt(identity):
            with sessions.begin() as session:
                row = session.get(BotDrawingTaskRow, identity)
                row.positive, row.prompt_status = 'a cat', 'ready'
        runner._expand_prompt = expand_prompt
    asyncio.run(runner._execute_online(task))
    assert seen[0]['size'] == 'auto' and seen[0]['quality'] == 'auto'
    assert seen[0]['output_format'] == ('gif' if gif else 'png')
    assert original in seen[0]['prompt'] and 'Avoid the following: 水印' in seen[0]['prompt']
    assert '【NP】' not in seen[0]['prompt']
    assert repo.task_for_worker(task)['generation_status'] == 'succeeded'


@pytest.mark.parametrize("with_reference", [False, True])
def test_mock_online_gif_is_saved_and_audited_without_losing_frames(sessions, tmp_path, monkeypatch, with_reference):
    service, _, _, repo = online_setup(sessions)
    media, task = DrawingMediaStore(tmp_path), uuid4()
    fields = {}
    if with_reference:
        reference, checksum = media.put(task, "input", image_bytes())
        fields = {"input_reference": reference, "input_sha256": checksum, "input_bytes": len(image_bytes())}
    task = repo.admit_online(service, task_id=task, source=source("131"), bot_self_id="456",
                             positive="生成一只跳舞的猫 GIF", **fields)
    assert repo.claim_online_next() == task
    output = BytesIO()
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue", "green")]
    frames[0].save(output, "GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    gif = output.getvalue()
    original_client, seen, audited = httpx.AsyncClient, [], []
    async def provider(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(gif).decode()}]})
    def mock_client(**kwargs):
        kwargs.pop("proxy", None)
        return original_client(transport=httpx.MockTransport(provider), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    class Auditor:
        async def audit(self, body):
            audited.append(body)
            return AuditDecision("allow", hashlib.sha256(body).hexdigest())
    runner = DrawingTaskRunner(repo, None, media, Auditor(), None, lambda _: None)
    asyncio.run(runner._execute_online(task))
    row = repo.task_for_worker(task)
    if with_reference:
        assert seen[0].url.path.endswith("/images/edits")
        assert b'name="output_format"\r\n\r\ngif' in seen[0].content
        assert b'name="size"\r\n\r\nauto' in seen[0].content
    else:
        assert seen[0].url.path.endswith("/images/generations")
        assert json.loads(seen[0].content)["output_format"] == "gif"
        assert json.loads(seen[0].content)["size"] == "auto"
    assert row["generation_status"] == "succeeded" and row["audit_status"] == "allow"
    assert row["output_reference"] == f"al1s-drawing/{task}/output.gif"
    assert audited == ([image_bytes(), gif] if with_reference else [gif])
    assert media.read(row["output_reference"], expected_sha256=row["output_sha256"]) == gif
    assert repo.begin_delete_tasks(service, [task], operation_id="delete-gif") == [task]
    assert asyncio.run(runner.delete_history([task]))["deleted"] == [str(task)]
    assert not (tmp_path / row["output_reference"]).exists()
    with sessions() as session:
        assert session.get(BotDrawingSettingsRow, service).media_bytes == 0


def test_gif_request_does_not_accept_a_static_provider_response(sessions, tmp_path, monkeypatch):
    service, _, _, repo = online_setup(sessions)
    task = repo.admit_online(service, task_id=uuid4(), source=source("133"), bot_self_id="456", positive="猫 GIF")
    assert repo.claim_online_next() == task
    original_client = httpx.AsyncClient
    async def provider(_request):
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image_bytes()).decode()}]})
    def mock_client(**kwargs):
        kwargs.pop("proxy", None)
        return original_client(transport=httpx.MockTransport(provider), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    runner = DrawingTaskRunner(repo, None, DrawingMediaStore(tmp_path), None, None, lambda _: None)
    asyncio.run(runner._execute_online(task))
    row = repo.task_for_worker(task)
    assert row["generation_status"] == "failed" and row["error_code"] == "drawing_online_output_invalid"
    assert row["online_request_sent"] and not row["online_image_received"] and not row["output_reference"]


def test_gif_intent_and_motion_survive_prompt_expansion(sessions, tmp_path, monkeypatch):
    service, _, preset, repo = online_setup(sessions)
    online = OnlinePresetWrite(name="线上绘图", model_id="gpt-image-2.6-flare", enabled=True, use_prompt_model=True)
    repo.save_online_preset(service, online, preset_id=UUID(preset["id"]), expected_version=1, operation_id="expand-gif")
    task = repo.admit_online(service, task_id=uuid4(), source=source("135"), bot_self_id="456",
                             positive="小猫跳舞的 GIF", negative="水印", original_positive="小猫跳舞的 GIF【NP】水印")
    assert repo.claim_online_next() == task
    output = BytesIO()
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
    frames[0].save(output, "GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    gif = output.getvalue()
    original_client, seen = httpx.AsyncClient, []
    async def provider(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(gif).decode()}]})
    def mock_client(**kwargs):
        kwargs.pop("proxy", None)
        return original_client(transport=httpx.MockTransport(provider), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    class Auditor:
        async def audit(self, body):
            return AuditDecision("allow", hashlib.sha256(body).hexdigest())
    runner = DrawingTaskRunner(repo, None, DrawingMediaStore(tmp_path), Auditor(), None, lambda _: None,
                               prompt_model=object())
    async def expand(identity):
        with sessions.begin() as session:
            row = session.get(BotDrawingTaskRow, identity)
            row.positive, row.prompt_status = "a cat", "ready"
    runner._expand_prompt = expand
    asyncio.run(runner._execute_online(task))
    assert seen[0]["output_format"] == "gif"
    assert "小猫跳舞的 GIF" in seen[0]["prompt"] and "【NP】" not in seen[0]["prompt"]
    assert repo.task_for_worker(task)["generation_status"] == "succeeded"


def test_failed_gif_output_reservation_can_be_cleaned_without_losing_accounting(sessions, tmp_path):
    from datetime import UTC, datetime, timedelta
    service, _, _, repo = online_setup(sessions)
    task = repo.admit_online(service, task_id=uuid4(), source=source("134"), bot_self_id="456", positive="猫 GIF")
    assert repo.claim_online_next() == task
    assert repo.online_state(task, "submitting") and repo.online_state(task, "sent") and repo.online_state(task, "received")
    output = BytesIO()
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
    frames[0].save(output, "GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    body = output.getvalue()
    row = repo.task_for_worker(task)
    repo.reserve_output(task, expected_version=row["row_version"], size=len(body), checksum=hashlib.sha256(body).hexdigest())
    media = DrawingMediaStore(tmp_path)
    reference, checksum = media.put(task, "output", body)
    assert repo.fail_online(task, "drawing_online_output_invalid", "failed")
    with sessions.begin() as session:
        session.get(BotDrawingTaskRow, task).media_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    runner = DrawingTaskRunner(repo, None, media, None, None, lambda _: None)
    asyncio.run(runner.cleanup_media(task))
    assert not (tmp_path / reference).exists()
    row = repo.task_for_worker(task)
    assert row["output_sha256"] is None and row["output_bytes"] is None
    with sessions() as session:
        assert session.get(BotDrawingSettingsRow, service).media_bytes == 0


def test_video_request_fails_before_prompt_expansion_or_provider_call(sessions, tmp_path, monkeypatch):
    service, _, _, repo = online_setup(sessions)
    task = repo.admit_online(service, task_id=uuid4(), source=source("132"), bot_self_id="456",
                             positive="生成一段猫咪视频")
    assert repo.claim_online_next() == task
    def unexpected(**_kwargs):
        raise AssertionError("unexpected provider request")
    monkeypatch.setattr(httpx, "AsyncClient", unexpected)
    runner = DrawingTaskRunner(repo, None, DrawingMediaStore(tmp_path), None, None, lambda _: None)
    asyncio.run(runner._execute_online(task))
    row = repo.task_for_worker(task)
    assert row["generation_status"] == "failed" and row["error_code"] == "drawing_video_unavailable"
    assert not row["online_request_sent"] and row["online_request_state"] == "not_started"
    assert row["cleanup_status"] == "done" and row["reply_kind"] == "failure_notice"


def test_online_edit_uses_reference_and_r18_does_not_add_green_restrictions(sessions, tmp_path, monkeypatch):
    service, _, _, repo = online_setup(sessions)
    with sessions.begin() as session:
        session.add(BotDrawingGroupModeRow(service_id=service, group_id="123", mode="r18"))
    media, identity, picture = DrawingMediaStore(tmp_path), uuid4(), image_bytes()
    reference, checksum = media.put(identity, "input", picture)
    task = repo.admit_online(service, task_id=identity, source=source("140"), bot_self_id="456",
        positive="把背景改成雪景", input_reference=reference, input_sha256=checksum, input_bytes=len(picture))
    assert repo.claim_online_next() == task
    original_client, requests = httpx.AsyncClient, []
    async def provider(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(picture).decode()}]})
    def mock_client(**kwargs):
        kwargs.pop("proxy", None)
        return original_client(transport=httpx.MockTransport(provider), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    class Auditor:
        async def audit(self, body):
            return AuditDecision("allow", hashlib.sha256(body).hexdigest())
    runner = DrawingTaskRunner(repo, None, media, Auditor(), None, lambda _: None)
    asyncio.run(runner._execute_online(task))
    assert requests[0].url.path.endswith("/images/edits")
    assert b'reference.png' in requests[0].content and b'Create only SFW content' not in requests[0].content
    row = repo.task_for_worker(task)
    assert row["input_audit_status"] == "allow" and row["audit_status"] == "allow"
    assert row["generation_status"] == "succeeded" and row["online_image_received"]


def test_late_provider_response_cannot_complete_cancelled_task(sessions, tmp_path, monkeypatch):
    service, _, _, repo = online_setup(sessions)
    task = admit(repo, service, "150")
    assert repo.claim_online_next() == task
    original_client = httpx.AsyncClient
    sent, release = asyncio.Event(), asyncio.Event()
    async def provider(request):
        await request.extensions["trace"]("http11.send_request_body.complete", {})
        sent.set()
        await release.wait()
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image_bytes()).decode()}]})
    def mock_client(**kwargs):
        kwargs.pop("proxy", None)
        return original_client(transport=httpx.MockTransport(provider), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    class Auditor:
        async def audit(self, body):
            return AuditDecision("allow", hashlib.sha256(body).hexdigest())
    runner = DrawingTaskRunner(repo, None, DrawingMediaStore(tmp_path), Auditor(), None, lambda _: None)
    async def run():
        pending = asyncio.create_task(runner._execute_online(task))
        await asyncio.wait_for(sent.wait(), timeout=5)
        assert await asyncio.to_thread(repo.request_cancel, service, task, operation_id="stop-race") == "cancelled"
        release.set()
        await asyncio.wait_for(pending, timeout=5)
    asyncio.run(run())
    row = repo.task_for_worker(task)
    assert row["generation_status"] == "cancelled" and row["online_request_sent"]
    assert not row["online_image_received"] and row["reply_kind"] != "image" and row["output_reference"] is None
