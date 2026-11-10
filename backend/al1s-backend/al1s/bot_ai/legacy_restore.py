"""Return AL1S-owned QQ conversations to a stopped legacy runtime."""

import json
import hashlib
from datetime import datetime
from datetime import UTC
from uuid import UUID


def _require_quiesced(check):
    if not check():
        raise ValueError('ai_restore_requires_stopped_writers')


def _origin(row):
    kind = 'GroupMessage' if row['scope_kind'] == 'qq_group' else 'FriendMessage'
    return f"al1s_qq_{row['service_id'].hex}:{kind}:{row['target_id']}"


def _marker(identity):
    return 'AL1S rollback ' + str(identity)


def _find_marker(database, origin, marker):
    rows = database.execute('select conversation_id from conversations where user_id=? and title=? limit 2',
                            (origin, marker)).fetchall()
    if len(rows) > 1:
        raise ValueError('ai_restore_creation_ambiguous')
    return str(UUID(rows[0][0])) if rows else None


def _owned(database, origin, identity):
    row = database.execute('select user_id from conversations where conversation_id=?', (identity,)).fetchone()
    if row is not None and row[0] != origin:
        raise ValueError('ai_restore_owner_conflict')
    return row is not None


def _datetime(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat(sep=' ')


def _restore_content(database, repository, row, origin, legacy_id, is_quiesced):
    if database.in_transaction:
        raise ValueError('ai_restore_database_busy')
    database.execute('BEGIN IMMEDIATE')
    try:
        database.execute('create temp table if not exists al1s_restore_messages '
                         '(position integer primary key, payload text not null)')
        database.execute('delete from al1s_restore_messages')
        cursor, position = 0, 0
        while True:
            _require_quiesced(is_quiesced)
            page = repository.messages_page(row['id'], after=cursor, limit=16)
            if not page:
                break
            values = []
            for sequence, messages in page:
                for message in messages:
                    values.append((position, json.dumps(message, ensure_ascii=False, separators=(',', ':'))))
                    position += 1
                cursor = sequence
            database.executemany('insert into al1s_restore_messages(position,payload) values (?,?)', values)
            if len(page) < 16:
                break
        _require_quiesced(is_quiesced)
        result = database.execute('''update conversations set content=(select json_group_array(json(payload))
            from (select payload from al1s_restore_messages order by position)), title=?, persona_id=?,
            token_usage=?, created_at=?, updated_at=? where conversation_id=? and user_id=?''',
            (row['title'], row['persona_id'], row['token_usage'], _datetime(row['created_at']),
             _datetime(row['updated_at']), legacy_id, origin))
        if result.rowcount != 1:
            raise ValueError('ai_restore_owner_conflict')
        database.commit()
    except BaseException:
        database.rollback()
        raise


async def _restore_one(database, manager, repository, row, is_quiesced):
    _require_quiesced(is_quiesced)
    origin = _origin(row)
    identity = row['legacy_id']
    if identity:
        identity = str(UUID(identity))
        exists = _owned(database, origin, identity)
    else:
        identity = _find_marker(database, origin, _marker(row['id']))
        exists = identity is not None
    if row['deleted_at'] is not None:
        if exists:
            await manager.delete_conversation(origin, identity)
        return
    if not identity:
        if not repository.claim_legacy_creation(row['id']):
            raise ValueError('ai_restore_creation_unknown')
        identity = str(UUID(await manager.new_conversation(origin, title=_marker(row['id']))))
        exists = _owned(database, origin, identity)
    if not exists:
        raise ValueError('ai_restore_legacy_missing')
    repository.map_legacy_id(row['id'], identity)
    _restore_content(database, repository, row, origin, identity, is_quiesced)


async def restore_legacy_history(database, manager, repository, service, *, is_quiesced):
    """Restore in bounded pages; uncertain new-conversation results are never replayed blindly."""
    count, after = 0, None
    while True:
        _require_quiesced(is_quiesced)
        rows = repository.conversations_page(service, after=after)
        if not rows:
            break
        for row in rows:
            await _restore_one(database, manager, repository, row, is_quiesced)
            count += 1
        after = rows[-1]['id']
        if len(rows) < 64:
            break
    after = None
    while True:
        _require_quiesced(is_quiesced)
        rows = repository.conversations_page(service, after=after)
        if not rows:
            break
        for row in rows:
            if row['current'] and row['deleted_at'] is None:
                identity = row['legacy_id']
                if not identity or not _owned(database, _origin(row), identity):
                    raise ValueError('ai_restore_selection_invalid')
                await manager.switch_conversation(_origin(row), identity)
        after = rows[-1]['id']
        if len(rows) < 64:
            break
    return {'conversations': count}


def restore_legacy_vision_jobs(database, repository, endpoints, *, is_quiesced):
    from al1s.bot_ai.managed.smart_contract import local_endpoint

    _require_quiesced(is_quiesced)
    owners, normalized = [], []
    for service, raw_endpoint in endpoints:
        raw_endpoint = raw_endpoint or 'http://host.docker.internal:8188'
        endpoint = local_endpoint(raw_endpoint)
        owner = 'al1s-vision-' + hashlib.sha256((service.hex + '|' + raw_endpoint).encode()).hexdigest()[:24]
        owners.append(owner)
        normalized.append((service, endpoint))
    if len(owners) != len(set(owners)):
        raise ValueError('ai_vision_owner_invalid')
    jobs = repository.vision_jobs_for(normalized)
    if database.in_transaction:
        raise ValueError('ai_restore_database_busy')
    columns = {row[1] for row in database.execute('pragma table_info(preferences)')}
    now = _datetime(datetime.now(UTC))
    database.execute('BEGIN IMMEDIATE')
    try:
        for owner, key in zip(owners, normalized, strict=True):
            _require_quiesced(is_quiesced)
            rows = database.execute("select count(*) from preferences where scope='al1s_vision' "
                                    "and scope_id=? and key='pending'", (owner,)).fetchone()[0]
            if rows > 1:
                raise ValueError('ai_vision_owner_invalid')
            value = json.dumps({'val': jobs[key]}, separators=(',', ':'))
            if rows:
                if 'updated_at' in columns:
                    database.execute("update preferences set value=?, updated_at=? where scope='al1s_vision' "
                                     "and scope_id=? and key='pending'", (value, now, owner))
                else:
                    database.execute("update preferences set value=? where scope='al1s_vision' "
                                     "and scope_id=? and key='pending'", (value, owner))
            elif 'created_at' in columns and 'updated_at' in columns:
                database.execute('insert into preferences(scope,scope_id,key,value,created_at,updated_at) '
                                 'values (?,?,?,?,?,?)', ('al1s_vision', owner, 'pending', value, now, now))
            else:
                database.execute('insert into preferences(scope,scope_id,key,value) values (?,?,?,?)',
                                 ('al1s_vision', owner, 'pending', value))
        _require_quiesced(is_quiesced)
        database.commit()
    except BaseException:
        database.rollback()
        raise
    return {'owners': len(owners), 'jobs': sum(len(value) for value in jobs.values())}
