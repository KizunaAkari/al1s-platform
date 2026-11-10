import json
import hashlib
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from al1s.adapters.postgres.bot_ai_history_models import AiConversationRow
from al1s.bot_ai.legacy_history import import_legacy_history, import_legacy_vision_jobs
from tests.test_bot_ai_history import history  # noqa: F401


def old_database():
    db = sqlite3.connect(':memory:')
    db.executescript('''
        create table conversations (inner_conversation_id integer primary key, conversation_id text,
          platform_id text, user_id text, content json, title text, persona_id text,
          token_usage integer, created_at text, updated_at text);
        create table preferences (scope text, scope_id text, key text, value json);
    ''')
    return db


def add_conversation(db, service, target, messages, *, selected=True):
    identity = uuid4()
    platform = f'al1s_qq_{service.hex}'
    origin = f'{platform}:GroupMessage:{target}'
    now = datetime.now(UTC).isoformat()
    db.execute('insert into conversations (conversation_id, platform_id, user_id, content, title, '
               'persona_id, token_usage, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?)',
               (str(identity), platform, origin, json.dumps(messages), '话题', 'persona-1', 42, now, now))
    if selected:
        db.execute('insert into preferences values (?, ?, ?, ?)',
                   ('umo', origin, 'sel_conv_id', json.dumps({'val': str(identity)})))
    db.commit()
    return identity, origin


def test_import_preserves_selection_history_and_clear_marker(history):
    repo, service, _ = history
    db = old_database()
    messages = [
        {'role': 'user', 'content': [{'type': 'text', 'text': '你好'}]},
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'type': 'function',
            'function': {'name': 'query_trading', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': '{}'},
        {'role': 'assistant', 'content': '收到'},
    ]
    identity, _ = add_conversation(db, service, '12345', messages)
    assert import_legacy_history(db, repo, {service}) == {'conversations': 1, 'turns': 1}
    context = repo.context(service, 'qq_group', '12345')
    assert context.id == identity and context.messages == messages
    with repo.sessions() as session:
        saved = session.get(AiConversationRow, identity)
        assert saved.legacy_id == str(identity) and saved.token_usage == 42 and saved.title == '话题'
    repo.clear(service, 'qq_group', '12345')
    import_legacy_history(db, repo, {service})
    assert repo.context(service, 'qq_group', '12345').messages == []


def test_preflight_rejects_unmapped_selection_before_importing_any_rows(history):
    repo, service, _ = history
    db = old_database()
    add_conversation(db, service, '111', [{'role': 'user', 'content': 'ok'}])
    _, origin = add_conversation(db, service, '222', [{'role': 'user', 'content': 'other'}])
    db.execute("update preferences set value=? where scope_id=?", (json.dumps({'val': str(uuid4())}), origin))
    db.commit()
    with pytest.raises(ValueError, match='selection'):
        import_legacy_history(db, repo, {service})
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AiConversationRow)) == 0


def test_preflight_rejects_incomplete_tool_call(history):
    repo, service, _ = history
    db = old_database()
    add_conversation(db, service, '111', [{'role': 'user', 'content': 'ok'}])
    add_conversation(db, service, '222', [{'role': 'user', 'content': 'other'},
        {'role': 'assistant', 'tool_calls': [{'id': 'pending'}], 'content': None}])
    with pytest.raises(ValueError, match='tool_incomplete'):
        import_legacy_history(db, repo, {service})
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AiConversationRow)) == 0


def test_vision_jobs_import_merges_without_overwriting_existing_or_duplicating(history):
    repo, service, _ = history
    db = old_database()
    endpoint = 'http://host.docker.internal:8188/'
    owner = 'al1s-vision-' + hashlib.sha256((service.hex + '|' + endpoint).encode()).hexdigest()[:24]
    old = {'prompt_id':str(uuid4()), 'created_at':datetime.now(UTC).timestamp()}
    new = {'prompt_id':str(uuid4()), 'created_at':datetime.now(UTC).timestamp()}
    db.execute('insert into preferences values (?, ?, ?, ?)',
               ('al1s_vision', owner, 'pending', json.dumps({'val':[old]})))
    db.commit()
    repo.save_vision_jobs(service, endpoint.rstrip('/'), [new])
    assert import_legacy_vision_jobs(db, repo, [(service, endpoint)]) == {'owners':1, 'jobs':1}
    import_legacy_vision_jobs(db, repo, [(service, endpoint)])
    assert {row['prompt_id'] for row in repo.vision_jobs(service, endpoint.rstrip('/'))} == {
        old['prompt_id'], new['prompt_id']}


def test_unknown_vision_owner_blocks_import_without_writes(history):
    repo, service, _ = history
    db = old_database()
    db.execute('insert into preferences values (?, ?, ?, ?)', ('al1s_vision', 'unknown', 'pending',
               json.dumps({'val':[{'prompt_id':str(uuid4()), 'created_at':1}]})))
    db.commit()
    with pytest.raises(ValueError, match='owner'):
        import_legacy_vision_jobs(db, repo, [(service, 'http://host.docker.internal:8188')])
    assert repo.vision_jobs(service, 'http://host.docker.internal:8188') == []
