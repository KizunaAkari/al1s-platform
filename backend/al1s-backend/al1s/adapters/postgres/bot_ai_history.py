from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from sqlalchemy import and_, delete, insert as bulk_insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from al1s.adapters.postgres.bot_ai_history_models import AiConversationRow as Conversation, AiMessageRow as Message, AiTurnRow as Turn, AiVisionJobRow as Job
from al1s.adapters.postgres.bot_ai_models import BotAiRequestRow as Request
from al1s.bot_ai.history import HistoryContext, VisionJob, digest, split_turns


def _vision_job_dict(row):
    result = dict(prompt_id=str(row.prompt_id), created_at=row.created_at.replace(tzinfo=UTC).timestamp())
    if row.boot_id is not None:
        result.update(boot_id=row.boot_id, execution_hash=row.execution_hash,
                      registration_state=row.registration_state, transport_started=row.transport_started)
    return result


def _vision_job_row(service, endpoint, raw):
    job = VisionJob.model_validate(raw)
    return dict(service_id=service, endpoint=endpoint, prompt_id=job.prompt_id,
                created_at=datetime.fromtimestamp(job.created_at, UTC), boot_id=job.boot_id,
                execution_hash=job.execution_hash, registration_state=job.registration_state,
                transport_started=job.transport_started)


class AiHistoryRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    def migration_sources(self):
        from al1s.adapters.postgres.bot_models import BotServiceRow
        from al1s.adapters.postgres.bot_ai_models import BotAiProviderRow, BotAiSettingsRow

        with self.sessions() as session:
            if session.scalar(select(Request.id).where(
                    Request.status.in_(['queued', 'running', 'stopping', 'blocked'])).limit(1)) is not None:
                raise ValueError('ai_transfer_requests_active')
            if session.scalar(select(BotAiSettingsRow.service_id).where(
                    BotAiSettingsRow.desired_version != BotAiSettingsRow.applied_version).limit(1)) is not None:
                raise ValueError('ai_transfer_settings_pending')
            if session.scalar(select(BotAiSettingsRow.service_id).where(
                    BotAiSettingsRow.enabled.is_(True)).limit(1)) is not None:
                raise ValueError('ai_transfer_admission_open')
            services = list(session.scalars(select(BotServiceRow.id).where(
                BotServiceRow.kind == 'onebot_gateway').limit(1001)))
            endpoints = list(session.execute(select(BotAiProviderRow.service_id, BotAiProviderRow.base_url)
                .join(BotServiceRow, BotServiceRow.id == BotAiProviderRow.service_id)
                .where(BotServiceRow.kind == 'onebot_gateway', BotAiProviderRow.role == 'vision',
                       BotAiProviderRow.adapter_kind == 'al1s_comfyui_vision').limit(101)))
        if len(services) > 1000 or len(endpoints) > 100:
            raise ValueError('ai_transfer_service_limit')
        return set(services), [(service, endpoint) for service, endpoint in endpoints]

    @staticmethod
    def _scope(service, scope, target):
        return (Conversation.service_id == service, Conversation.scope_kind == scope,
                Conversation.target_id == target)

    def context(self, service, scope, target) -> HistoryContext:
        with self.sessions.begin() as session:
            query = select(Conversation).where(*self._scope(service, scope, target), Conversation.current)
            row = session.scalar(query)
            if row is None:
                now = datetime.now(UTC)
                session.execute(insert(Conversation).values(id=uuid4(), service_id=service,
                    scope_kind=scope, target_id=target, current=True, revision=1, next_turn=1,
                    created_at=now, updated_at=now, token_usage=0).on_conflict_do_nothing())
                row = session.scalar(query)
            recent = (select(Turn.request_id).where(Turn.conversation_id == row.id)
                      .order_by(Turn.sequence.desc()).limit(9))
            messages = session.scalars(select(Message.payload).join(Turn, Turn.request_id == Message.turn_id)
                .where(Message.turn_id.in_(recent)).order_by(Turn.sequence, Message.ordinal)).all()
            return HistoryContext(row.id, row.revision, service, scope, target, list(messages))

    @staticmethod
    def _write_turns(session, conversation_id, values):
        turns, messages = [], []
        for request_id, sequence, created_at, content in values:
            turns.append(dict(request_id=request_id, conversation_id=conversation_id,
                              sequence=sequence, created_at=created_at, content_digest=digest(content)))
            messages.extend(dict(turn_id=request_id, ordinal=i, role=item['role'], payload=item)
                            for i, item in enumerate(content))
        for table, rows in ((Turn, turns), (Message, messages)):
            for start in range(0, len(rows), 500):
                session.execute(bulk_insert(table), rows[start:start + 500])

    def append(self, context, request_id, messages, *, epoch, config_version):
        if len(split_turns(messages)) != 1:
            raise ValueError('ai_history_round_invalid')
        with self.sessions.begin() as session:
            row = session.scalar(select(Conversation).where(Conversation.id == context.id,
                *self._scope(context.service_id, context.scope, context.target)).with_for_update())
            if row is None or not row.current or row.revision != context.revision:
                raise ValueError('ai_history_stale')
            previous = session.get(Turn, request_id)
            if previous:
                if previous.conversation_id != row.id or previous.content_digest != digest(messages):
                    raise ValueError('ai_history_conflict')
                return
            now = datetime.now(UTC)
            request = session.scalar(select(Request.id).where(Request.id == request_id,
                Request.service_id == context.service_id, Request.scope_kind == context.scope,
                Request.target_id == context.target, Request.status == 'running',
                Request.epoch == epoch, Request.config_version == config_version,
                Request.expires_at > now).with_for_update())
            if request is None:
                raise ValueError('ai_history_stale')
            self._write_turns(session, row.id, [(request_id, row.next_turn, now, messages)])
            row.next_turn += 1
            row.updated_at = now

    def clear(self, service, scope, target):
        self._clear(service, scope, target, delete_current=False)

    def delete(self, service, scope, target):
        self._clear(service, scope, target, delete_current=True)

    def _clear(self, service, scope, target, *, delete_current):
        with self.sessions.begin() as session:
            row = session.scalar(select(Conversation).where(*self._scope(service, scope, target),
                                                           Conversation.current).with_for_update())
            if row is None:
                return
            turns = select(Turn.request_id).where(Turn.conversation_id == row.id)
            session.execute(delete(Message).where(Message.turn_id.in_(turns)))
            session.execute(delete(Turn).where(Turn.conversation_id == row.id))
            row.revision += 1
            row.next_turn = 1
            row.token_usage = 0
            row.cleared_at = row.updated_at = datetime.now(UTC)
            if delete_current:
                row.deleted_at = row.cleared_at
                row.current = False

    def map_legacy_id(self, conversation_id, legacy_id):
        identity = str(UUID(str(legacy_id)))
        try:
            with self.sessions.begin() as session:
                row = session.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
                if row is None:
                    raise ValueError('ai_history_not_found')
                if row.legacy_id and row.legacy_id != identity:
                    raise ValueError('ai_history_legacy_conflict')
                row.legacy_id = identity
                row.legacy_create_pending = False
        except IntegrityError:
            raise ValueError('ai_history_legacy_conflict') from None

    def claim_legacy_creation(self, conversation_id):
        with self.sessions.begin() as session:
            row = session.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
            if row is None:
                raise ValueError('ai_history_not_found')
            if row.legacy_id or row.legacy_create_pending:
                return False
            row.legacy_create_pending = True
            return True

    def conversations_page(self, service, *, after=None, limit=64):
        if not 1 <= limit <= 100:
            raise ValueError('ai_history_page_limit')
        query = select(Conversation).where(Conversation.service_id == service)
        if after is not None:
            query = query.where(Conversation.id > after)
        with self.sessions() as session:
            rows = session.scalars(query.order_by(Conversation.id).limit(limit)).all()
            fields = ('id', 'service_id', 'scope_kind', 'target_id', 'current', 'legacy_id',
                      'legacy_create_pending', 'title', 'persona_id', 'token_usage', 'created_at',
                      'updated_at', 'deleted_at')
            return [{key: getattr(row, key) for key in fields} for row in rows]

    def messages_page(self, conversation_id, *, after=0, limit=100):
        if not 1 <= limit <= 500:
            raise ValueError('ai_history_page_limit')
        with self.sessions() as session:
            turns = (select(Turn.request_id).where(Turn.conversation_id == conversation_id,
                Turn.sequence > after).order_by(Turn.sequence).limit(limit))
            rows = session.execute(select(Turn.sequence, Message.payload).join(Message,
                Message.turn_id == Turn.request_id).where(Turn.request_id.in_(turns))
                .order_by(Turn.sequence, Message.ordinal)).all()
            grouped = {}
            for sequence, payload in rows:
                grouped.setdefault(sequence, []).append(payload)
            return list(grouped.items())

    def import_conversation(self, service, scope, target, legacy_id, messages, *, current,
                            created_at, title=None, persona_id=None, token_usage=0, updated_at=None):
        self.import_batch([dict(service=service, scope=scope, target=target, legacy_id=legacy_id,
            messages=messages, current=current, created_at=created_at, updated_at=updated_at,
            title=title, persona_id=persona_id, token_usage=token_usage)])
        return UUID(str(legacy_id))

    def import_batch(self, records):
        if not 1 <= len(records) <= 64:
            raise ValueError('ai_history_import_batch_invalid')
        prepared = [(item, UUID(str(item['legacy_id'])), split_turns(item['messages']),
                     digest(item['messages'])) for item in records]
        if len({identity for _, identity, _, _ in prepared}) != len(prepared):
            raise ValueError('ai_history_import_conflict')
        with self.sessions.begin() as session:
            existing = {row.id: row for row in session.scalars(select(Conversation).where(
                Conversation.id.in_([identity for _, identity, _, _ in prepared])).with_for_update())}
            pending = []
            for item, identity, turns, fingerprint in prepared:
                previous = existing.get(identity)
                if previous and (previous.service_id != item['service'] or previous.scope_kind != item['scope']
                        or previous.target_id != item['target'] or previous.import_digest != fingerprint):
                    raise ValueError('ai_history_import_conflict')
                if not previous:
                    pending.append((item, identity, turns, fingerprint))
            selections = [self._scope(item['service'], item['scope'], item['target'])
                          for item, _, _, _ in pending if item['current']]
            if selections:
                from sqlalchemy import and_, or_
                session.execute(update(Conversation).where(Conversation.current,
                    or_(*(and_(*scope) for scope in selections))).values(
                        current=False, revision=Conversation.revision + 1))
            session.add_all([Conversation(id=identity, service_id=item['service'],
                scope_kind=item['scope'], target_id=item['target'], current=item['current'],
                revision=1, next_turn=len(turns) + 1, legacy_id=str(item['legacy_id']),
                import_digest=fingerprint, title=item.get('title'), persona_id=item.get('persona_id'),
                token_usage=item.get('token_usage', 0), created_at=item['created_at'],
                updated_at=item.get('updated_at') or item['created_at'])
                for item, identity, turns, fingerprint in pending])
            session.flush()
            turn_values = []
            for item, identity, turns, _ in pending:
                turn_values.extend((identity, uuid5(identity, f'legacy-turn-{i}'), i,
                                    item['created_at'], content)
                                   for i, content in enumerate(turns, 1))
            self._write_import_turns(session, turn_values)

    @staticmethod
    def _write_import_turns(session, values):
        turns, messages = [], []
        for conversation, request, sequence, created, content in values:
            turns.append(dict(request_id=request, conversation_id=conversation, sequence=sequence,
                              created_at=created, content_digest=digest(content)))
            messages.extend(dict(turn_id=request, ordinal=i, role=message['role'], payload=message)
                            for i, message in enumerate(content))
        for table, rows in ((Turn, turns), (Message, messages)):
            for start in range(0, len(rows), 500):
                session.execute(bulk_insert(table), rows[start:start + 500])

    def vision_jobs(self, service, endpoint):
        with self.sessions() as session:
            rows = session.scalars(select(Job).where(Job.service_id == service, Job.endpoint == endpoint)
                                   .order_by(Job.created_at, Job.prompt_id).limit(16)).all()
            return [_vision_job_dict(row) for row in rows]

    def vision_jobs_for(self, endpoints):
        if len(endpoints) > 100 or len(set(endpoints)) != len(endpoints):
            raise ValueError('ai_vision_job_limit')
        if not endpoints:
            return {}
        result = {item: [] for item in endpoints}
        with self.sessions() as session:
            rows = session.scalars(select(Job).where(or_(*(and_(Job.service_id == service,
                Job.endpoint == endpoint) for service, endpoint in endpoints)))
                .order_by(Job.service_id, Job.endpoint, Job.created_at, Job.prompt_id)).all()
            for row in rows:
                key = (row.service_id, row.endpoint)
                result[key].append(_vision_job_dict(row))
            if any(len(jobs) > 16 for jobs in result.values()):
                raise ValueError('ai_vision_job_limit')
        return result

    def save_vision_jobs(self, service, endpoint, jobs):
        from al1s.bot_ai.managed.comfy_client import local_endpoint
        local_endpoint(endpoint)
        if len(jobs) > 16:
            raise ValueError('ai_vision_job_limit')
        values = [_vision_job_row(service, endpoint, item) for item in jobs]
        with self.sessions.begin() as session:
            session.execute(delete(Job).where(Job.service_id == service, Job.endpoint == endpoint))
            if values:
                session.execute(bulk_insert(Job), values)

    def import_vision_jobs(self, batches):
        from al1s.bot_ai.history import VisionJob as _Job
        from al1s.bot_ai.managed.smart_contract import local_endpoint

        values, owners = [], set()
        for service, endpoint, jobs in batches:
            endpoint = local_endpoint(endpoint)
            owner = (service, endpoint)
            if owner in owners or len(jobs) > 16:
                raise ValueError('ai_vision_job_limit')
            owners.add(owner)
            for raw in jobs:
                job = _Job.model_validate(raw)
                values.append(_vision_job_row(service, endpoint, job.model_dump(mode='json')))
        if len(values) > 1600 or len(owners) > 100:
            raise ValueError('ai_vision_job_limit')
        if not owners:
            return
        with self.sessions.begin() as session:
            existing = session.scalars(select(Job).where(or_(*(and_(Job.service_id == service,
                Job.endpoint == endpoint) for service, endpoint in owners))).with_for_update()).all()
            seen = {(row.service_id, row.endpoint, row.prompt_id): row.created_at.replace(tzinfo=UTC)
                    for row in existing}
            counts = {owner: 0 for owner in owners}
            for row in existing:
                counts[(row.service_id, row.endpoint)] += 1
            new = []
            for row in values:
                identity = (row['service_id'], row['endpoint'], row['prompt_id'])
                if identity in seen:
                    if abs((seen[identity] - row['created_at']).total_seconds()) > 0.000001:
                        raise ValueError('ai_vision_job_conflict')
                    continue
                seen[identity] = row['created_at']
                counts[identity[:2]] += 1
                if counts[identity[:2]] > 16:
                    raise ValueError('ai_vision_job_limit')
                new.append(row)
            if new:
                session.execute(bulk_insert(Job), new)
