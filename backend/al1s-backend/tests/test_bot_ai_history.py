from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.bot_models import BotServiceRow
from al1s.adapters.postgres.bot_ai_models import BotAiRequestRow
from al1s.adapters.postgres.bot_ai_history import AiHistoryRepository
from al1s.adapters.postgres.bot_ai_history_models import (
    AiConversationRow, AiTurnRow, AiMessageRow, AiVisionJobRow,
)
from al1s.bot_ai.history import parse_origin, split_turns
from tests.test_quant_settings import sqlite_jsonb  # noqa: F401


@pytest.fixture
def history():
    engine = create_engine('sqlite://')
    for model in (BotServiceRow, BotAiRequestRow, AiConversationRow, AiTurnRow, AiMessageRow, AiVisionJobRow):
        model.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = uuid4()
    with sessions.begin() as session:
        session.add(BotServiceRow(id=service, kind='onebot_gateway', name='QQ', enabled=True,
                                 row_version=1))
    yield AiHistoryRepository(sessions), service, engine
    engine.dispose()


def turn(text):
    return [{'role': 'user', 'content': text}, {'role': 'assistant', 'content': '答' + text}]


def active_request(repo, context, *, status='running', version=1):
    request_id = uuid4()
    now = datetime.now(UTC)
    with repo.sessions.begin() as session:
        session.add(BotAiRequestRow(id=request_id, service_id=context.service_id,
            scope_kind=context.scope, target_id=context.target, sender_id='1',
            source_message_id=request_id.hex, config_version=version, status=status,
            send_status='none', epoch='epoch1', created_at=now,
            expires_at=now + timedelta(minutes=5), sent_count=0))
    return request_id


def append(repo, context, messages):
    request = active_request(repo, context)
    repo.append(context, request, messages, epoch='epoch1', config_version=1)
    with repo.sessions.begin() as session:
        session.get(BotAiRequestRow, request).status = 'completed'
    return request


def test_stopped_or_stale_request_cannot_append_to_history(history):
    repo, service, _ = history
    context = repo.context(service, 'qq_group', '12345')
    request = active_request(repo, context, status='stopping')
    with pytest.raises(ValueError, match='stale'):
        repo.append(context, request, turn('迟到'), epoch='epoch1', config_version=1)
    with repo.sessions.begin() as session:
        session.get(BotAiRequestRow, request).status = 'running'
    with pytest.raises(ValueError, match='stale'):
        repo.append(context, request, turn('旧版本'), epoch='epoch1', config_version=2)
    with pytest.raises(ValueError, match='stale'):
        repo.append(context, request, turn('旧实例'), epoch='epoch2', config_version=1)
    repo.append(context, request, turn('有效'), epoch='epoch1', config_version=1)
    assert repo.context(service, 'qq_group', '12345').messages == turn('有效')


def test_identity_and_rounds_preserve_tool_pairs():
    service = uuid4()
    assert parse_origin(f'al1s_qq_{service.hex}:GroupMessage:12345') == (service, 'qq_group', '12345')
    with pytest.raises(ValueError):
        parse_origin('qq:GroupMessage:12345')
    messages = [turn('一')[0], {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'call-1', 'type': 'function', 'function': {'name': 'query_trading', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': '{}'}, turn('一')[1]]
    assert split_turns(messages + turn('二')) == [messages, turn('二')]
    with pytest.raises(ValueError, match='tool'):
        split_turns([turn('一')[0], messages[2]])
    with pytest.raises(ValueError, match='tool'):
        split_turns(messages[:2])


def test_recent_window_is_bounded_and_full_history_remains(history):
    repo, service, engine = history
    context = repo.context(service, 'qq_group', '12345')
    for n in range(1000):
        append(repo, context, turn(str(n)))
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        current = repo.context(service, 'qq_group', '12345')
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert len(statements) <= 3
    assert current.messages == sum((turn(str(n)) for n in range(991, 1000)), [])
    assert len(repo.messages_page(context.id, after=0, limit=100)) == 100
    with repo.sessions() as session:
        assert len(session.scalars(select(AiTurnRow)).all()) == 1000


def test_clear_fences_late_write_and_import_does_not_resurrect(history):
    repo, service, _ = history
    old = uuid4()
    repo.import_conversation(service, 'qq_group', '12345', old, turn('旧'), current=True,
                             created_at=datetime.now(UTC), title='旧话题')
    context = repo.context(service, 'qq_group', '12345')
    request = append(repo, context, turn('新'))
    repo.append(context, request, turn('新'), epoch='epoch1', config_version=1)
    with pytest.raises(ValueError, match='conflict'):
        repo.append(context, request, turn('不同'), epoch='epoch1', config_version=1)
    repo.clear(service, 'qq_group', '12345')
    with pytest.raises(ValueError, match='stale'):
        repo.append(context, uuid4(), turn('迟到'), epoch='epoch1', config_version=1)
    repo.import_conversation(service, 'qq_group', '12345', old, turn('旧'), current=True,
                             created_at=datetime.now(UTC), title='旧话题')
    cleared = repo.context(service, 'qq_group', '12345')
    assert cleared.messages == [] and cleared.revision > context.revision
    with repo.sessions() as session:
        assert session.get(AiConversationRow, old).cleared_at is not None


def test_service_scope_and_visual_jobs_are_isolated(history):
    repo, service, _ = history
    group = repo.context(service, 'qq_group', '12345')
    private = repo.context(service, 'qq_private', '12345')
    append(repo, group, turn('群'))
    assert repo.context(service, 'qq_private', '12345').messages == []
    assert private.id != group.id
    endpoint = 'http://host.docker.internal:8188'
    jobs = [{'prompt_id': str(uuid4()), 'created_at': datetime.now(UTC).timestamp()} for _ in range(16)]
    repo.save_vision_jobs(service, endpoint, jobs)
    assert repo.vision_jobs(service, endpoint) == jobs
    with pytest.raises(ValueError, match='limit'):
        repo.save_vision_jobs(service, endpoint, jobs + [jobs[0]])
    assert repo.vision_jobs(service, endpoint) == jobs
    repo.save_vision_jobs(service, endpoint, jobs[1:])
    assert repo.vision_jobs(service, endpoint) == jobs[1:]


def test_deleted_conversation_retains_tombstone_and_new_session_has_new_identity(history):
    repo, service, _ = history
    old = repo.context(service, 'qq_group', '12345')
    append(repo, old, turn('旧内容'))
    repo.delete(service, 'qq_group', '12345')
    current = repo.context(service, 'qq_group', '12345')
    assert current.id != old.id and current.messages == []
    with repo.sessions() as session:
        removed = session.get(AiConversationRow, old.id)
        assert removed.deleted_at is not None and not removed.current
    with pytest.raises(ValueError, match='stale'):
        repo.append(old, uuid4(), turn('迟到'), epoch='epoch1', config_version=1)


def test_legacy_alias_mapping_is_idempotent_and_rejects_conflicts(history):
    repo, service, _ = history
    context = repo.context(service, 'qq_group', '12345')
    legacy_id = str(uuid4())
    repo.map_legacy_id(context.id, legacy_id)
    repo.map_legacy_id(context.id, legacy_id)
    with pytest.raises(ValueError, match='conflict'):
        repo.map_legacy_id(context.id, str(uuid4()))
