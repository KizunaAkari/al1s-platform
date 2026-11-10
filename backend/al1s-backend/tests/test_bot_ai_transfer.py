import importlib.util
import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from al1s.bot_ai.transfer_import import import_snapshot
from tests.test_bot_ai_legacy_history import add_conversation, old_database


def test_transfer_imports_a_complete_snapshot_without_exposing_content():
    service = uuid4()
    database = old_database()
    add_conversation(database, service, '100', [{'role': 'user', 'content': 'fixture'}])

    class Repository:
        batches = []
        def migration_sources(self):
            return {service}, []
        def import_batch(self, values):
            self.batches.append(values)
        def import_vision_jobs(self, values):
            assert values == []

    repository = Repository()
    result = import_snapshot(database.serialize(), repository)
    assert result == {'history': {'conversations': 1, 'turns': 1}, 'vision': {'owners': 0, 'jobs': 0}}
    assert len(repository.batches) == 1
    with pytest.raises(ValueError, match='snapshot_size'):
        import_snapshot(b'', repository)


def test_transfer_requires_the_reviewed_qq_writer_to_be_stopped(monkeypatch):
    script = Path(__file__).resolve().parents[3] / 'al1s-deployment/scripts/qq-unified-transfer.py'
    spec = importlib.util.spec_from_file_location('qq_unified_transfer', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    image, identity = 'sha256:' + 'a' * 64, 'b' * 64
    row = {'Id': identity, 'Image': image, 'State': {'Running': True, 'Status': 'running'}, 'Config': {'Labels': {
        'com.docker.compose.project': 'al1s-bot', 'com.docker.compose.service': 'llbot'}}}
    monkeypatch.setattr(module, 'inspect', lambda _name: row)
    with pytest.raises(ValueError, match='writer_not_stopped'):
        module.stopped_qq(identity, image)
    row['State']['Running'] = False
    row['State']['Status'] = 'exited'
    module.stopped_qq(identity, image)
    row['State']['Restarting'] = True
    with pytest.raises(ValueError, match='writer_not_stopped'):
        module.stopped_qq(identity, image)
    row['State']['Restarting'] = False
    with pytest.raises(ValueError, match='writer_not_stopped'):
        module.stopped_qq('c' * 64, image)


def test_transfer_creates_a_consistent_private_sqlite_snapshot(tmp_path, monkeypatch):
    import shutil

    script = Path(__file__).resolve().parents[3] / 'al1s-deployment/scripts/qq-unified-transfer.py'
    spec = importlib.util.spec_from_file_location('qq_unified_transfer_snapshot', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source, output = tmp_path / 'source.db', tmp_path / 'snapshot.db'
    with sqlite3.connect(source) as database:
        database.execute('create table sample(value text)')
        database.execute("insert into sample values ('fixture')")
    def copy(source_name, destination, *, optional=False):
        if optional:
            return False
        shutil.copyfile(source, destination)
        return True
    monkeypatch.setattr(module, 'copy_stopped_file', copy)
    data = module.snapshot(output)
    assert data == output.read_bytes()
    with sqlite3.connect(output) as database:
        assert database.execute('select value from sample').fetchone() == ('fixture',)


def test_wal_snapshot_can_be_deserialized_without_the_original_database(tmp_path, monkeypatch):
    import shutil

    script = Path(__file__).resolve().parents[3] / 'al1s-deployment/scripts/qq-unified-transfer.py'
    spec = importlib.util.spec_from_file_location('qq_unified_transfer_wal', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source, output = tmp_path / 'wal.db', tmp_path / 'snapshot.db'
    database = sqlite3.connect(source)
    assert database.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal'
    database.execute('create table sample(value text)')
    database.execute("insert into sample values ('wal fixture')")
    database.commit()
    def copy(source_name, destination, *, optional=False):
        path = Path(str(source) + '-wal') if optional else source
        if not path.exists():
            return False
        shutil.copyfile(path, destination)
        return True
    monkeypatch.setattr(module, 'copy_stopped_file', copy)
    data = module.snapshot(output)
    memory = sqlite3.connect(':memory:')
    try:
        memory.deserialize(data)
        assert memory.execute('PRAGMA quick_check').fetchone() == ('ok',)
        assert memory.execute('select value from sample').fetchone() == ('wal fixture',)
    finally:
        memory.close()
        database.close()


def test_restore_waits_for_sticker_imports_and_file_recovery():
    script = Path(__file__).resolve().parents[3] / 'al1s-deployment/scripts/qq-unified-restore.py'
    spec = importlib.util.spec_from_file_location('qq_unified_restore', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    database = sqlite3.connect(':memory:')
    database.execute('create table meta(key text primary key, value text)')
    database.execute('insert into meta values (?,?)', ('al1s_import_jobs', json.dumps([{'id': 'pending'}])))
    with pytest.raises(ValueError, match='sticker_import_pending'):
        module.check_sticker_state(database.serialize())
    database.execute('update meta set value=?', ('[]',))
    database.execute('insert into meta values (?,?)', ('al1s_file_operation', '{"operation":"move"}'))
    with pytest.raises(ValueError, match='sticker_recovery_pending'):
        module.check_sticker_state(database.serialize())
    database.execute("update meta set value='' where key='al1s_file_operation'")
    module.check_sticker_state(database.serialize())


def test_source_assembly_rejects_changed_workspace_without_overwriting_it(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[3] / 'al1s-deployment/bots/prepare_qq_unified_source.py'
    spec = importlib.util.spec_from_file_location('qq_unified_source', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / 'backend' / 'al1s' / 'sample.py'
    source.parent.mkdir(parents=True)
    source.write_text('user edit\n', encoding='utf-8')
    baseline, patch = tmp_path / 'baseline.json', tmp_path / 'baseline.patch'
    patch.write_bytes(b'')
    baseline.write_text(json.dumps({'baseline_image_id':
        'sha256:9bb1abb8e712ae2d006b8acd64cf1738f2dce58eb86d20073e51d2040ecaa496',
        'patch_sha256': hashlib.sha256(b'').hexdigest(), 'files': {'sample.py': {
            'workspace_sha256': hashlib.sha256(b'old\n').hexdigest(), 'baseline_sha256': 'unused'}}}), encoding='utf-8')
    artifacts = tmp_path / 'artifacts' / 'qq-unified'
    artifacts.mkdir(parents=True)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'ARTIFACTS', artifacts)
    monkeypatch.setattr(module, 'SOURCE', tmp_path / 'backend')
    monkeypatch.setattr(module, 'BASELINE', baseline)
    monkeypatch.setattr(module, 'PATCH', patch)
    monkeypatch.setattr('sys.argv', ['prepare_qq_unified_source.py', str(artifacts / 'candidate')])
    with pytest.raises(ValueError, match='qq_workspace_source_changed'):
        module.main()
    assert source.read_text(encoding='utf-8') == 'user edit\n'


def test_restore_cleanup_only_stops_the_matching_temporary_worker(monkeypatch):
    from types import SimpleNamespace

    script = Path(__file__).resolve().parents[3] / 'al1s-deployment/scripts/qq-unified-restore.py'
    spec = importlib.util.spec_from_file_location('qq_unified_restore_cleanup', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity, name, image = uuid4(), 'al1s-qq-unified-restore-fixture', 'sha256:' + 'a' * 64
    row = {'Id': 'container-id', 'Name': '/' + name, 'Image': image, 'State': {'Running': True},
        'Config': {'Labels': {'com.docker.compose.project': 'al1s-bot',
            'com.docker.compose.service': 'llbot', 'al1s.qq-unified-restore': str(identity)}}}
    calls = []
    def run(command, **_):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps([row]).encode(), stderr=b'')
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert module.cleanup_worker(name, identity, image) == 'stopped'
    assert calls[-1] == ['docker', 'stop', '--time', '30', 'container-id']
    row['Config']['Labels']['al1s.qq-unified-restore'] = 'other'
    with pytest.raises(ValueError, match='worker_identity_changed'):
        module.cleanup_worker(name, identity, image)
    assert sum(command[1] == 'stop' for command in calls) == 1
