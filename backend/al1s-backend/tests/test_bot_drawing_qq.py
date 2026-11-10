import asyncio
import time
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4
from PIL import Image

from al1s.bot_drawing.contracts import PromptIntent
from al1s.bot_drawing.service import DrawingService


def event(body, *, at=True):
    segments = ([{"type": "at", "data": {"qq": "100"}}] if at else [])
    segments.append({"type": "text", "data": {"text": body}})
    return {"post_type": "message", "message_type": "group", "self_id": 100, "user_id": 200,
            "group_id": 300, "message_id": 123, "time": int(time.time()), "message": segments}


class Client:
    def post(self, action, _payload):
        assert action == 'get_login_info'
        return {'user_id': 100}


def fixture(mode='direct', duplicate=False):
    service_id, profile_id, calls = uuid4(), uuid4(), []
    row = SimpleNamespace(service_id=service_id, event_url='ws://bot')
    settings = {'enabled': True, 'row_version': 1, 'applied_version': 1, 'event_ws_url': 'ws://bot'}
    class Repo:
        def get_settings(self, *_): return settings
        def allowed(self, *_): return True
        def current_mode(self, *_): return 'green'
        def find_source(self, *_): return uuid4() if duplicate else None
        def find_control(self, *_): return None
        def list_presets(self, *_): return []
        def prepare_route(self, service, source, intent, kind, force_default=False, workflow_number=None):
            calls.append(('route', kind, force_default))
            if workflow_number is not None: calls.append(('workflow_number', workflow_number))
            choice = SimpleNamespace(id=uuid4(), revision=1, number=workflow_number or 1, lora_keys=())
            return SimpleNamespace(chat_profile_id=profile_id, group_mode='green',
                decision=SimpleNamespace(status='selected', workflow=choice, ignored_lora_keys=()))
        def get_preset(self, *_): return {'name': '图生图'}
        def admit(self, service, source, own, endpoint, prepared, **values):
            calls.append(('admit', source.sender_id, values['positive']))
            if values.get('wish_text'):
                calls.append(('wish', values['wish_text']))
            return uuid4()
    class Manager:
        async def interpret_drawing(self, *_):
            calls.append(('interpret',)); return PromptIntent(mode=mode, positive='(cat:1.2)' if mode == 'direct' else '')
        async def expand_drawing(self, *_args, **_kwargs):
            calls.append(('expand',)); return '(cat:1.2)', ''
    class Runner:
        def wake(self): calls.append(('wake',))
    service = DrawingService(Repo(), Manager(), None, None, Runner())
    return service, row, calls


def test_ordinary_addressed_chat_skips_drawing_model():
    async def run():
        service, row, calls = fixture(mode='chat')
        assert await service.accept(row, Client(), event('你好，武尊')) is False
        assert calls == []
    asyncio.run(run())


def test_admin_preview_splits_inline_negative_for_local_and_online_presets():
    async def run():
        for engine in ('comfy', 'openai'):
            service, row, calls = fixture()
            service.repository.get_preset = lambda *_: {'row_version': 1, 'engine': engine, 'kind': 'txt2img'}
            def admit(*_args, **kwargs):
                calls.append(('conditions', kwargs['positive'], kwargs['negative']))
                return kwargs['task_id']
            service.repository.admit_preview = admit
            service.repository.admit_online = admit
            await service.submit_preview(row.service_id, uuid4(), positive='cat【负面】rain', negative='blur',
                mode='r18', image=None, expected_version=1, operation_id='test-preview')
            assert ('conditions', 'cat', 'rain, blur') in calls
    asyncio.run(run())


def test_local_wish_preserves_marked_negative_and_original_text():
    async def run():
        service, row, calls = fixture()
        def admit(_service, _source, _own, _endpoint, _prepared, **values):
            calls.append(('conditions', values['positive'], values['negative'], values['wish_text']))
            return uuid4()
        service.repository.admit = admit
        assert await service.accept(row, Client(), event('/wf3 一只猫【负向】水印', at=False))
        assert ('conditions', '', '水印', '一只猫【负向】水印') in calls
        assert ('expand',) not in calls
    asyncio.run(run())


def test_workflow_prefix_uses_explicit_number_without_intent_classification():
    async def run():
        service, row, calls = fixture()
        assert await service.accept(row, Client(), event('/wf3 正面：blue cat', at=False)) is True
        assert ('workflow_number', 3) in calls
        assert ('route', 'txt2img', False) in calls
        assert ('admit', '200', 'blue cat') in calls
        assert ('interpret',) not in calls and ('expand',) not in calls
    asyncio.run(run())


def test_workflow_prefix_image_edit_uses_explicit_number(monkeypatch):
    from contextlib import nullcontext
    from al1s.bot_drawing import service as drawing_service
    async def run():
        service, row, calls = fixture()
        async def reference(*_): return b'image'
        monkeypatch.setattr(drawing_service, 'resolve_reference', reference)
        class Media:
            def staging(self): return nullcontext()
            def put(self, task_id, _kind, _content): return f'al1s-drawing/{task_id}/input.png', 'a' * 64
        service.media = Media()
        item = event('/wf5 把衣服改红', at=False)
        item['message'].insert(0, {'type': 'reply', 'data': {'id': '456'}})
        assert await service.accept(row, Client(), item) is True
        assert ('workflow_number', 5) in calls and ('route', 'image_edit', False) in calls
        assert ('interpret',) not in calls and ('expand',) not in calls
        assert ('wish', '把衣服改红') in calls
    asyncio.run(run())


def test_explicit_workflow_errors_do_not_fall_back_to_another_preset():
    async def run():
        for status, expected in [('selection_unavailable', '不可用'), ('kind_mismatch', '不匹配')]:
            service, row, calls = fixture()
            original = service.repository.prepare_route
            def prepare(*args, **kwargs):
                result = original(*args, **kwargs)
                result.decision = SimpleNamespace(status=status, workflow=None)
                return result
            async def control(_row, _client, _command, _version, _action, text, **_kwargs):
                calls.append(('message', text))
            service.repository.prepare_route = prepare
            service._control = control
            assert await service.accept(row, Client(), event('/wf3 正面：blue cat', at=False)) is True
            assert any(item[0] == 'message' and expected in item[1] for item in calls)
            assert not any(item[0] == 'admit' for item in calls)
    asyncio.run(run())


def test_group_restricted_workflow_reports_denial_without_expansion_or_enqueue():
    async def run():
        service, row, calls = fixture()
        def denied(*_args, **_kwargs):
            raise ValueError('drawing_group_preset_denied')
        async def control(_row, _client, _command, _version, _action, text, **_kwargs):
            calls.append(('message', text))
        service.repository.prepare_route = denied
        service._control = control
        assert await service.accept(row, Client(), event('/wf3 请画一只猫', at=False)) is True
        assert ('message', '该群未启用此工作流。') in calls
        assert not any(item[0] in {'expand', 'admit'} for item in calls)
    asyncio.run(run())


def test_denied_group_workflow_does_not_depend_on_a_chat_profile():
    async def run():
        service, row, calls = fixture()
        def denied(*_args, **_kwargs): raise ValueError('drawing_group_preset_denied')
        async def control(_row, _client, _command, _version, _action, text, **_kwargs): calls.append(('message', text))
        service.repository.prepare_route = denied
        service._control = control
        assert await service.accept(row, Client(), event('/wf3 画一只猫', at=False)) is True
        assert ('message', '该群未启用此工作流。') in calls
        assert not any(item[0] in {'expand', 'admit'} for item in calls)
    asyncio.run(run())


def test_workflow_list_uses_the_current_group_scope():
    async def run():
        service, row, calls = fixture()
        def scoped(_service, source):
            calls.append(('scope', source.target_id))
            return [{'number': 1, 'name': '可用预设', 'kind': 'txt2img', 'enabled': True, 'validation_state': 'ready'}]
        async def control(_row, _client, _command, _version, _action, text, **_kwargs): calls.append(('message', text))
        service.repository.list_scope_presets = scoped
        service._control = control
        assert await service.accept(row, Client(), event('/wf', at=False)) is True
        assert ('scope', '300') in calls
        assert any('可用预设' in item[1] for item in calls if item[0] == 'message')
    asyncio.run(run())


def test_p2p_denied_for_group_does_not_expand_or_enqueue(monkeypatch):
    from al1s.bot_drawing import service as drawing_service
    async def run():
        service, row, calls = fixture()
        async def reference(*_): return b'image'
        monkeypatch.setattr(drawing_service, 'resolve_reference', reference)
        def denied(*_args, **kwargs):
            assert kwargs['force_default'] is True
            raise ValueError('drawing_group_preset_denied')
        async def control(_row, _client, _command, _version, _action, text, **_kwargs): calls.append(('message', text))
        service.repository.prepare_route = denied
        service._control = control
        item = event('/p2p 改成红色', at=False)
        item['message'].insert(0, {'type': 'reply', 'data': {'id': '456'}})
        assert await service.accept(row, Client(), item) is True
        assert ('message', '该群未启用此工作流。') in calls
        assert not any(item[0] in {'expand', 'admit'} for item in calls)
    asyncio.run(run())


def test_addressed_persona_command_bypasses_drawing_intent():
    async def run():
        for command in ('切换人格 武尊', '切换人格'):
            service, row, calls = fixture(mode='chat')
            assert await service.accept(row, Client(), event(command)) is False
            assert calls == []
    asyncio.run(run())


def test_openai_request_error_produces_safe_group_notice(caplog):
    import pytest
    from al1s.bot_drawing.prompt_model import DrawingPromptModel

    class FakeApiError(Exception):
        pass

    async def run():
        service, _row, _calls = fixture()
        class Provider:
            async def generate(self, *_args, **_kwargs):
                raise FakeApiError('private provider response')
        service.repository.prompt_credentials = lambda *_: (
            SimpleNamespace(adapter_kind='openai_chat_completion', model_id='draw-model'), 'draw-key')
        manager = DrawingPromptModel(service.repository, client_factory=lambda *_: object(),
                                     provider_factory=lambda *_: Provider())
        with pytest.raises(ValueError, match='drawing_llm_provider_error'):
            await manager.expand_drawing(uuid4(), uuid4(), 1, '画一只猫', PromptIntent(mode='wish'),
                                         workflow_name='测试', aliases=[])
        assert 'private provider response' not in caplog.text
    asyncio.run(run())


def test_direct_prompt_is_admitted_once_for_the_real_sender():
    async def run():
        service, row, calls = fixture()
        assert await service.accept(row, Client(), event('/wf3 正面：(cat:1.2)')) is True
        assert ('admit', '200', '(cat:1.2)') in calls and ('wake',) in calls
    asyncio.run(run())


def test_ordinary_unaddressed_group_message_does_not_call_llm():
    async def run():
        service, row, calls = fixture()
        assert await service.accept(row, Client(), event('画一只猫', at=False)) is False
        assert calls == []
    asyncio.run(run())


def test_duplicate_qq_event_does_not_call_llm_or_create_another_task():
    async def run():
        service, row, calls = fixture(duplicate=True)
        assert await service.accept(row, Client(), event('/wf3 画一只猫')) is True
        assert calls == []
    asyncio.run(run())


def test_duplicate_control_reply_does_not_repeat_intent_detection():
    async def run():
        service, row, calls = fixture()
        service.repository.find_control = lambda *_: ('receipt-key', {'status': 'sent', 'text': '已完成'})
        assert await service.accept(row, Client(), event('/wf3 画一只猫')) is True
        assert calls == []
    asyncio.run(run())


def test_p2p_uses_member_reply_and_forces_default_image_workflow(monkeypatch):
    from al1s.bot_drawing import commands
    async def run():
        service, row, calls = fixture()
        buffer = BytesIO(); Image.new('RGB', (8, 8), 'blue').save(buffer, 'PNG'); body = buffer.getvalue()
        monkeypatch.setattr(commands, 'read_media', lambda _reference: body)
        class ReplyClient(Client):
            def post(self, action, payload):
                if action == 'get_login_info': return super().post(action, payload)
                assert action == 'get_msg'
                return {'message_type': 'group', 'group_id': 300, 'sender': {'user_id': 201},
                        'message': [{'type': 'image', 'data': {'url': 'https://example.test/image.png'}}]}
        class Media:
            def staging(self):
                from contextlib import nullcontext
                return nullcontext()
            def put(self, task_id, kind, content):
                assert kind == 'input' and content == body
                return f'al1s-drawing/{task_id}/input.png', 'a' * 64
        service.media = Media()
        item = event('/p2p 换背景', at=False)
        item['message'].insert(0, {'type': 'reply', 'data': {'id': '456'}})
        assert await service.accept(row, ReplyClient(), item)
        assert ('route', 'image_edit', True) in calls and ('wake',) in calls
        assert ('interpret',) not in calls and ('expand',) not in calls
        assert ('wish', '换背景') in calls
    asyncio.run(run())


def test_explicit_p2p_with_multiple_images_returns_usage_without_generating():
    async def run():
        service, row, calls = fixture()
        async def control(_row, _client, _command, _version, _action, text, **_):
            calls.append(('message', text))
        service._control = control
        item = event('/p2p 换背景')
        item['message'].extend([{'type': 'image', 'data': {'url': 'https://example.test/one.png'}},
                                {'type': 'image', 'data': {'url': 'https://example.test/two.png'}}])
        assert await service.accept(row, Client(), item)
        assert len(calls) == 1 and '回复当前会话中的单张图片' in calls[0][1]
    asyncio.run(run())


def test_p2p_queues_wish_before_prompt_expansion(monkeypatch):
    from contextlib import nullcontext
    from al1s.bot_drawing import service as drawing_service
    async def run():
        service, row, calls = fixture()
        async def reference(*_): return b'image'
        monkeypatch.setattr(drawing_service, 'resolve_reference', reference)
        class Media:
            def staging(self): return nullcontext()
            def put(self, task_id, _kind, _body): return f'al1s-drawing/{task_id}/input.png', 'a' * 64
        service.media = Media()
        item = event('/p2p 换背景', at=False)
        item['message'].insert(0, {'type': 'reply', 'data': {'id': '456'}})
        assert await service.accept(row, Client(), item)
        assert ('wish', '换背景') in calls
        assert ('expand',) not in calls and any(value[0] == 'admit' for value in calls)
    asyncio.run(run())


def test_p2p_explicit_positive_and_negative_bypass_llm(monkeypatch):
    from contextlib import nullcontext
    from al1s.bot_drawing import service as drawing_service
    async def run():
        service, row, calls = fixture()
        async def reference(*_): return b'image'
        monkeypatch.setattr(drawing_service, 'resolve_reference', reference)
        class Media:
            def staging(self): return nullcontext()
            def put(self, task_id, _kind, _content): return f'al1s-drawing/{task_id}/input.png', 'a' * 64
        service.media = Media()
        def admit(_service, _source, _own, _endpoint, _prepared, **values):
            calls.append(('conditions', values['positive'], values['negative']))
            return uuid4()
        service.repository.admit = admit
        item = event('/p2p 正面：cat 负面：rain', at=False)
        item['message'].insert(0, {'type': 'reply', 'data': {'id': '456'}})
        assert await service.accept(row, Client(), item)
        assert ('conditions', 'cat', 'rain') in calls
        assert ('interpret',) not in calls and ('expand',) not in calls
    asyncio.run(run())


def test_r18_explicit_prompt_works_without_any_chat_profile():
    async def run():
        service, row, calls = fixture()
        service.repository.current_mode = lambda *_: 'r18'
        async def control(_row, _client, _command, _version, _action, text, **_): calls.append(('message', text))
        service._control = control
        original_prepare = service.repository.prepare_route
        def prepare(*args, **kwargs):
            result = original_prepare(*args, **kwargs)
            result.chat_profile_id, result.group_mode = None, 'r18'
            return result
        service.repository.prepare_route = prepare
        assert await service.accept(row, Client(), event('/wf3 正面：blue hair, white dress')) is True
        assert ('admit', '200', 'blue hair, white dress') in calls
        assert ('interpret',) not in calls and ('expand',) not in calls
    asyncio.run(run())


def test_r18_prefixed_structured_tags_bypass_classifier_and_unprefixed_chat_skips_drawing():
    async def run():
        service, row, calls = fixture()
        service.repository.current_mode = lambda *_: 'r18'
        original_prepare = service.repository.prepare_route
        def prepare(*args, **kwargs):
            result = original_prepare(*args, **kwargs)
            result.group_mode = 'r18'
            return result
        service.repository.prepare_route = prepare
        assert await service.accept(row, Client(), event('/wf3 (masterpiece:1.2), 1girl, blue hair')) is True
        assert ('admit', '200', '(masterpiece:1.2), 1girl, blue hair') in calls
        assert ('interpret',) not in calls and ('expand',) not in calls
        calls.clear()
        service, row, calls = fixture(mode='chat')
        service.repository.current_mode = lambda *_: 'r18'
        assert await service.accept(row, Client(), event('你好，最近怎么样')) is False
        assert calls == []
    asyncio.run(run())


def test_r18_bypass_aborts_if_group_changes_to_green_before_route_is_prepared():
    async def run():
        service, row, calls = fixture()
        service.repository.current_mode = lambda *_: 'r18'
        async def control(_row, _client, _command, _version, _action, text, **_): calls.append(('message', text))
        service._control = control
        assert await service.accept(row, Client(), event('/wf3 正面：a blue cat')) is True
        assert not any(item[0] == 'admit' for item in calls)
        assert any(item[0] == 'message' for item in calls)
    asyncio.run(run())


def test_r18_p2p_direct_fields_preserve_reference_without_chat_profile(monkeypatch):
    from contextlib import nullcontext
    from al1s.bot_drawing import service as drawing_service
    async def run():
        service, row, calls = fixture()
        service.repository.current_mode = lambda *_: 'r18'
        original_prepare = service.repository.prepare_route
        def prepare(*args, **kwargs):
            result = original_prepare(*args, **kwargs)
            result.chat_profile_id, result.group_mode = None, 'r18'
            return result
        service.repository.prepare_route = prepare
        async def reference(*_): return b'image'
        monkeypatch.setattr(drawing_service, 'resolve_reference', reference)
        class Media:
            def staging(self): return nullcontext()
            def put(self, task_id, _kind, _content): return f'al1s-drawing/{task_id}/input.png', 'a' * 64
        service.media = Media()
        def admit(_service, _source, _own, _endpoint, _prepared, **values):
            calls.append(('conditions', values['positive'], values['negative'], values['reference_message_id']))
            return uuid4()
        service.repository.admit = admit
        item = event('/p2p prompt: make the dress red\nnegative: blur', at=False)
        item['message'].insert(0, {'type': 'reply', 'data': {'id': '456'}})
        assert await service.accept(row, Client(), item)
        assert ('conditions', 'make the dress red', 'blur', '456') in calls
        assert ('route', 'image_edit', True) in calls and ('interpret',) not in calls and ('expand',) not in calls
    asyncio.run(run())


def test_green_prefixed_bare_tags_are_direct_without_model_call():
    async def run():
        service, row, calls = fixture()
        assert await service.accept(row, Client(), event('/wf3 (masterpiece:1.2), 1girl, blue hair'))
        assert ('interpret',) not in calls and ('expand',) not in calls
        assert ('admit', '200', '(masterpiece:1.2), 1girl, blue hair') in calls
    asyncio.run(run())
