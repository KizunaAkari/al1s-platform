"""Read an inactive AstrBot SQLite snapshot into the AL1S chat repository."""

import json
import hashlib
from datetime import UTC, datetime
from uuid import UUID

from al1s.bot_ai.history import parse_origin, split_turns

_BATCH = 64


def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _selections(database, allowed_services):
    query = '''select p.scope_id, p.value, c.user_id from preferences p
        left join conversations c on c.conversation_id =
          case when json_valid(p.value) then json_extract(p.value, '$.val') end
        where p.scope='umo' and p.key='sel_conv_id' and p.scope_id like 'al1s_qq_%' '''
    result = {}
    for origin, raw, owner in database.execute(query):
        service, _, _ = parse_origin(origin)
        try:
            selected = UUID(str(json.loads(raw)['val']))
        except (ValueError, TypeError, KeyError):
            raise ValueError('ai_history_selection_invalid') from None
        if service not in allowed_services or owner != origin or origin in result:
            raise ValueError('ai_history_selection_invalid')
        result[origin] = selected
    return result


def _batches(database, allowed_services, selected):
    query = '''select conversation_id, platform_id, user_id, content, title, persona_id,
                      token_usage, created_at, updated_at
               from conversations where user_id like 'al1s_qq_%' order by inner_conversation_id'''
    cursor = database.execute(query)
    while rows := cursor.fetchmany(_BATCH):
        batch = []
        for identity, platform, origin, content, title, persona, tokens, created, updated in rows:
            service, scope, target = parse_origin(origin)
            if service not in allowed_services or platform != origin.split(':', 1)[0]:
                raise ValueError('ai_history_origin_invalid')
            identity = UUID(str(identity))
            messages = json.loads(content) if content else []
            if not isinstance(messages, list):
                raise ValueError('ai_history_messages_invalid')
            turns = split_turns(messages)
            batch.append(dict(service=service, scope=scope, target=target, legacy_id=identity,
                messages=messages, current=selected.get(origin) == identity,
                created_at=_timestamp(created), updated_at=_timestamp(updated), title=title,
                persona_id=persona, token_usage=tokens, _turn_count=len(turns)))
        yield batch


def import_legacy_history(database, repository, allowed_services):
    """Preflight the full snapshot, then import idempotently in bounded transactions."""
    database.execute('SAVEPOINT al1s_history_import')
    try:
        selected = _selections(database, allowed_services)
        counts = {'conversations': 0, 'turns': 0}
        for batch in _batches(database, allowed_services, selected):
            counts['conversations'] += len(batch)
            counts['turns'] += sum(row['_turn_count'] for row in batch)
        for batch in _batches(database, allowed_services, selected):
            repository.import_batch(batch)
        database.execute('RELEASE al1s_history_import')
        return counts
    except Exception:
        database.execute('ROLLBACK TO al1s_history_import')
        database.execute('RELEASE al1s_history_import')
        raise


def import_legacy_vision_jobs(database, repository, endpoints):
    from al1s.bot_ai.history import VisionJob as _Job
    from al1s.bot_ai.managed.smart_contract import local_endpoint

    owners = {}
    for service, raw_endpoint in endpoints:
        endpoint = local_endpoint(raw_endpoint)
        raw_endpoint = raw_endpoint or 'http://host.docker.internal:8188'
        owner = 'al1s-vision-' + hashlib.sha256((service.hex + '|' + raw_endpoint).encode()).hexdigest()[:24]
        if owner in owners and owners[owner] != (service, endpoint):
            raise ValueError('ai_vision_owner_invalid')
        owners[owner] = (service, endpoint)
    batches, identities = [], set()
    rows = database.execute("select scope_id, value from preferences where scope='al1s_vision' and key='pending'")
    for owner, raw in rows:
        try:
            jobs = json.loads(raw)['val']
            if not isinstance(jobs, list) or len(jobs) > 16:
                raise ValueError('ai_vision_job_limit')
            jobs = [_Job.model_validate(job).model_dump(mode='json') for job in jobs]
        except (ValueError, TypeError, KeyError):
            raise ValueError('ai_vision_job_invalid') from None
        if not jobs:
            continue
        if owner not in owners or owners[owner] in identities:
            raise ValueError('ai_vision_owner_invalid')
        identities.add(owners[owner])
        batches.append((*owners[owner], jobs))
    repository.import_vision_jobs(batches)
    return {'owners': len(batches), 'jobs': sum(len(batch[2]) for batch in batches)}
