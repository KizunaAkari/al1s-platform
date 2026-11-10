import asyncio
import json
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from al1s.bot_ai.legacy_restore import restore_legacy_history, restore_legacy_vision_jobs
from tests.test_bot_ai_history import append, history, turn  # noqa: F401
from tests.test_bot_ai_legacy_history import add_conversation, old_database


class Manager:
    def __init__(self, db, *, fail_after_create=False):
        self.db, self.fail_after_create, self.creates = db, fail_after_create, 0

    async def new_conversation(self, origin, *, title):
        self.creates += 1
        identity = str(uuid4())
        now = datetime.now(UTC).isoformat()
        self.db.execute('insert into conversations (conversation_id,platform_id,user_id,content,title,'
            'token_usage,created_at,updated_at) values (?,?,?,?,?,?,?,?)',
            (identity, origin.split(':')[0], origin, '[]', title, 0, now, now))
        self.db.commit()
        if self.fail_after_create:
            self.fail_after_create = False
            raise TimeoutError('simulated lost receipt')
        return identity

    async def switch_conversation(self, origin, identity):
        self.db.execute("delete from preferences where scope='umo' and scope_id=? and key='sel_conv_id'", (origin,))
        self.db.execute('insert into preferences values (?,?,?,?)',
                        ('umo', origin, 'sel_conv_id', json.dumps({'val':identity})))
        self.db.commit()

    async def delete_conversation(self, origin, identity):
        self.db.execute('delete from conversations where user_id=? and conversation_id=?', (origin, identity))
        self.db.execute("delete from preferences where scope='umo' and scope_id=? and key='sel_conv_id'", (origin,))
        self.db.commit()


def test_restore_does_not_resurrect_cleared_content(history):
    async def run():
        repo, service, _ = history
        db = old_database()
        legacy_id, origin = add_conversation(db, service, '100', turn('旧'))
        repo.import_conversation(service, 'qq_group', '100', legacy_id, turn('旧'), current=True,
                                 created_at=datetime.now(UTC))
        repo.clear(service, 'qq_group', '100')
        manager = Manager(db)
        await restore_legacy_history(db, manager, repo, service, is_quiesced=lambda:True)
        assert json.loads(db.execute('select content from conversations where conversation_id=?',
                                    (str(legacy_id),)).fetchone()[0]) == []
        assert json.loads(db.execute('select value from preferences where scope_id=?', (origin,)).fetchone()[0])['val'] == str(legacy_id)
    asyncio.run(run())


def test_restore_recovers_unknown_new_conversation_receipt_without_duplicate(history):
    async def run():
        repo, service, _ = history
        db = old_database()
        context = repo.context(service, 'qq_group', '100')
        append(repo, context, turn('新'))
        manager = Manager(db, fail_after_create=True)
        with pytest.raises(TimeoutError):
            await restore_legacy_history(db, manager, repo, service, is_quiesced=lambda:True)
        await restore_legacy_history(db, manager, repo, service, is_quiesced=lambda:True)
        assert manager.creates == 1 and db.execute('select count(*) from conversations').fetchone()[0] == 1
        assert json.loads(db.execute('select content from conversations').fetchone()[0]) == turn('新')
    asyncio.run(run())


def test_restore_deletion_removes_old_conversation_without_recreating_it(history):
    async def run():
        repo, service, _ = history
        db = old_database()
        legacy_id, _ = add_conversation(db, service, '100', turn('旧'))
        repo.import_conversation(service, 'qq_group', '100', legacy_id, turn('旧'), current=True,
                                 created_at=datetime.now(UTC))
        repo.delete(service, 'qq_group', '100')
        manager = Manager(db)
        await restore_legacy_history(db, manager, repo, service, is_quiesced=lambda:True)
        assert db.execute('select count(*) from conversations').fetchone()[0] == 0 and manager.creates == 0
    asyncio.run(run())


def test_restore_does_not_retry_an_unknown_creation_without_a_marker(history):
    async def run():
        repo, service, _ = history
        db = old_database()
        repo.context(service, 'qq_group', '100')
        class Unknown(Manager):
            async def new_conversation(self, origin, *, title):
                self.creates += 1
                raise TimeoutError('result unavailable')
        manager = Unknown(db)
        with pytest.raises(TimeoutError):
            await restore_legacy_history(db, manager, repo, service, is_quiesced=lambda:True)
        with pytest.raises(ValueError, match='creation_unknown'):
            await restore_legacy_history(db, manager, repo, service, is_quiesced=lambda:True)
        assert manager.creates == 1
    asyncio.run(run())


def test_restore_vision_replaces_old_owned_jobs_but_preserves_other_owners(history):
    repo, service, _ = history
    db = old_database()
    endpoint = 'http://host.docker.internal:8188/'
    owner = 'al1s-vision-' + hashlib.sha256((service.hex + '|' + endpoint).encode()).hexdigest()[:24]
    old = {'prompt_id':str(uuid4()),'created_at':1}
    current = {'prompt_id':str(uuid4()),'created_at':2}
    db.execute('insert into preferences values (?,?,?,?)', ('al1s_vision', owner, 'pending', json.dumps({'val':[old]})))
    db.execute('insert into preferences values (?,?,?,?)', ('al1s_vision', 'other-owner', 'pending', json.dumps({'val':[old]})))
    db.commit()
    repo.save_vision_jobs(service, endpoint.rstrip('/'), [current])
    restore_legacy_vision_jobs(db, repo, [(service, endpoint)], is_quiesced=lambda:True)
    assert json.loads(db.execute('select value from preferences where scope_id=?', (owner,)).fetchone()[0])['val'] == [current]
    assert json.loads(db.execute("select value from preferences where scope_id='other-owner'").fetchone()[0])['val'] == [old]
