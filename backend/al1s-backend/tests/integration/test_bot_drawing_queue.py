from datetime import UTC, datetime, timedelta

import pytest

from al1s.adapters.postgres.bot_drawing_models import BotDrawingTaskRow
from al1s.adapters.postgres.bot_drawing_repository import BotDrawingRepository
from al1s.bot_drawing.contracts import PromptIntent
from al1s.meme_workshop.contracts import CommandSource
from tests.integration.test_bot_drawing_persistence import add_preset, add_service, make_task, ready_for_draw, sessions

pytestmark = pytest.mark.integration


def request(message, sender='1'):
    return CommandSource(message_id=message, scope_kind='qq_group', target_id='123', sender_id=sender)


def test_same_user_can_queue_multiple_drawings_without_replaying_a_duplicate_event(sessions):
    service, _ = ready_for_draw(sessions)
    repo = BotDrawingRepository(sessions)
    intent = PromptIntent(mode='direct', positive='cat')
    first, second = request('100'), request('101')
    first_route = repo.prepare_route(service, first, intent, 'txt2img')
    second_route = repo.prepare_route(service, second, intent, 'txt2img')
    one = repo.admit(service, first, '456', 'test', first_route, positive='cat', negative='', seed=1)
    two = repo.admit(service, second, '456', 'test', second_route, positive='cat', negative='', seed=2)
    assert one != two
    assert repo.admit(service, first, '456', 'test', first_route, positive='cat', negative='', seed=1) == one
    with sessions() as session:
        assert session.get(BotDrawingTaskRow, one).generation_status == 'queued'
        assert session.get(BotDrawingTaskRow, two).generation_status == 'queued'


def test_missing_wish_model_fails_only_its_task_and_releases_next_gpu_turn(sessions):
    import asyncio
    from al1s.bot_ai.gpu_queue import GpuQueue
    from al1s.bot_drawing.runner import DrawingTaskRunner

    service, _ = ready_for_draw(sessions)
    repo = BotDrawingRepository(sessions)
    wish, direct = request('201'), request('202')
    first_intent, second_intent = PromptIntent(mode='wish'), PromptIntent(mode='direct', positive='cat')
    first = repo.admit(service, wish, '456', 'test', repo.prepare_route(service, wish, first_intent, 'txt2img'),
                       positive='', negative='', seed=1, wish_text='画一只猫')
    second = repo.admit(service, direct, '456', 'test', repo.prepare_route(service, direct, second_intent, 'txt2img'),
                        positive='cat', negative='', seed=2)
    queue = GpuQueue()
    runner = DrawingTaskRunner(repo, None, None, None, None, lambda _: None, gpu_queue=queue, prompt_model=object())
    async def run():
        queue.reserve(str(first)); queue.reserve(str(second))
        assert await runner.register(first) == 'prompting'
        await asyncio.gather(*runner._prompt_tasks.values())
    asyncio.run(run())
    failed = repo.task_for_worker(first)
    assert failed['generation_status'] == 'failed' and failed['error_code'] == 'drawing_prompt_model_unconfigured'
    assert failed['cleanup_status'] == 'done' and queue.active == str(second)
    assert repo.task_for_worker(second)['generation_status'] == 'queued'


def test_claim_uses_gpu_reservation_order_when_database_commits_in_opposite_order(sessions):
    with sessions.begin() as session:
        service = add_service(session, 'reservation-order')
        preset = add_preset(session, service)
        first = make_task(service, preset, '1', '100')
        second = make_task(service, preset, '2', '101')
        first.created_at = datetime.now(UTC) + timedelta(seconds=1)
        second.created_at = datetime.now(UTC)
        session.add_all([first, second])
        first_id, second_id = first.id, second.id
    repo = BotDrawingRepository(sessions)
    assert repo.next_registration('test') == second_id
    assert repo.next_registration('test', expected_task_id=first_id) == first_id
    with sessions.begin() as session:
        session.get(BotDrawingTaskRow, first_id).resource_registration_state = 'confirmed'
        session.get(BotDrawingTaskRow, second_id).resource_registration_state = 'confirmed'
    claimed = repo.claim_next('test', expected_task_id=first_id)
    assert claimed is not None and claimed.id == first_id


def test_database_capacity_counts_drawing_and_tag_together(sessions):
    service, preset = ready_for_draw(sessions)
    with sessions.begin() as session:
        for index in range(6):
            item = make_task(service, preset, str(index + 1), str(100 + index))
            if index < 5:
                item.operation, item.preset_id, item.preset_revision, item.dependency_signature = 'tag', None, None, None
            session.add(item)
    repo = BotDrawingRepository(sessions)
    source = request('999', '1000')
    intent = PromptIntent(mode='direct', positive='cat')
    route = repo.prepare_route(service, source, intent, 'txt2img')
    with pytest.raises(ValueError, match='drawing_queue_full'):
        repo.admit(service, source, '456', 'test', route, positive='cat', negative='', seed=1)


def test_unregistered_queued_task_can_acknowledge_while_prior_cleanup_waits(sessions):
    service, preset = ready_for_draw(sessions)
    with sessions.begin() as session:
        old = make_task(service, preset, '1', '99')
        old.generation_status, old.cleanup_status = 'succeeded', 'deferred'
        old.send_status, old.notice_status = 'sent', 'sent'
        session.add(old)
    repo, source = BotDrawingRepository(sessions), request('100')
    route = repo.prepare_route(service, source, PromptIntent(mode='direct', positive='cat'), 'txt2img')
    task = repo.admit(service, source, '456', 'test', route, positive='cat', negative='', seed=1)
    assert repo.next_registration('test') is None
    assert repo.next_delivery('test') == (task, 'notice')
    assert repo.start_send(task, 'notice')
    assert not repo.start_send(task, 'notice')
    assert repo.mark_transport_started(task, 'notice', None)
    assert repo.finish_send(task, 'notice', 'sent', 1, 'receipt')
    assert repo.next_delivery('test') is None


def test_notice_is_not_transmitted_if_task_finishes_after_notice_claim(sessions):
    service, _ = ready_for_draw(sessions)
    repo, source = BotDrawingRepository(sessions), request('100')
    route = repo.prepare_route(service, source, PromptIntent(mode='direct', positive='cat'), 'txt2img')
    task = repo.admit(service, source, '456', 'test', route, positive='cat', negative='', seed=1)
    assert repo.start_send(task, 'notice')
    with sessions.begin() as session:
        row = session.get(BotDrawingTaskRow, task)
        row.generation_status, row.send_status, row.reply_kind = 'failed', 'ready', 'failure_notice'
    assert not repo.mark_transport_started(task, 'notice', None)
    assert repo.finish_send(task, 'notice', 'failed', 0, None)


def test_unknown_early_notice_is_not_resent(sessions):
    service, _ = ready_for_draw(sessions)
    repo, source = BotDrawingRepository(sessions), request('100')
    route = repo.prepare_route(service, source, PromptIntent(mode='direct', positive='cat'), 'txt2img')
    task = repo.admit(service, source, '456', 'test', route, positive='cat', negative='', seed=1)
    assert repo.start_send(task, 'notice') and repo.mark_transport_started(task, 'notice', None)
    assert repo.finish_send(task, 'notice', 'unknown', 0, None)
    assert repo.next_delivery('test') is None and not repo.start_send(task, 'notice')


def test_expired_queued_task_does_not_send_stale_notice(sessions):
    service, _ = ready_for_draw(sessions)
    repo, source = BotDrawingRepository(sessions), request('100')
    route = repo.prepare_route(service, source, PromptIntent(mode='direct', positive='cat'), 'txt2img')
    task = repo.admit(service, source, '456', 'test', route, positive='cat', negative='', seed=1)
    with sessions.begin() as session:
        session.get(BotDrawingTaskRow, task).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert repo.next_delivery('test') is None and not repo.start_send(task, 'notice')
