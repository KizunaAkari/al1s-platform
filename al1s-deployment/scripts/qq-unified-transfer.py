"""Run inside the existing ops container after the QQ writer has stopped."""

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from al1s.components.host.config import HostSettings
from al1s.components.host.journal import HostJournal

LEGACY = '/var/lib/al1s-astrbot/data/data_v4.db'
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024


def inspect(name):
    result = subprocess.run(['docker', 'inspect', name], capture_output=True, check=True)
    rows = json.loads(result.stdout)
    if len(rows) != 1:
        raise ValueError('qq_transfer_container_ambiguous')
    return rows[0]


def stopped_qq(container_id, image_id):
    row = inspect('al1s-bot-qq')
    labels = row['Config'].get('Labels') or {}
    if (row['Id'] != container_id or row['Image'] != image_id or row['State']['Running']
            or row['State'].get('Restarting') or row['State'].get('Status') not in {'exited', 'created'}
            or labels.get('com.docker.compose.project') != 'al1s-bot'
            or labels.get('com.docker.compose.service') != 'llbot'):
        raise ValueError('qq_transfer_writer_not_stopped')


def platform(image_id):
    row = inspect('al1s-platform-app')
    labels = row['Config'].get('Labels') or {}
    if (not row['State']['Running'] or row['State'].get('Paused') or row['Image'] != image_id
            or labels.get('com.docker.compose.project') != 'al1s-platform'
            or labels.get('com.docker.compose.service') != 'backend'):
        raise ValueError('qq_transfer_platform_changed')


def copy_stopped_file(source, destination, *, optional=False):
    result = subprocess.run(['docker', 'cp', 'al1s-bot-qq:' + source, str(destination)],
                            capture_output=True)
    if result.returncode:
        if optional and b'Could not find the file ' in result.stderr:
            return False
        raise ValueError('qq_transfer_snapshot_copy_failed')
    return True


def snapshot(destination, *, source_path=LEGACY):
    with tempfile.TemporaryDirectory(prefix='al1s-qq-transfer-') as temporary:
        source = Path(temporary) / 'data_v4.db'
        copy_stopped_file(source_path, source)
        copy_stopped_file(source_path + '-wal', Path(str(source) + '-wal'), optional=True)
        database = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
        normalized = Path(temporary) / 'normalized.sqlite3'
        copy = sqlite3.connect(str(normalized))
        try:
            size = database.execute('PRAGMA page_count').fetchone()[0] * database.execute('PRAGMA page_size').fetchone()[0]
            if size <= 0 or size > MAX_SNAPSHOT_BYTES:
                raise ValueError('qq_transfer_snapshot_size_invalid')
            database.backup(copy)
            if copy.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() != 'delete':
                raise ValueError('qq_transfer_snapshot_journal_invalid')
            if copy.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('qq_transfer_snapshot_invalid')
        finally:
            copy.close()
            database.close()
        data = normalized.read_bytes()
        if not data or len(data) > MAX_SNAPSHOT_BYTES:
            raise ValueError('qq_transfer_snapshot_size_invalid')
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return data


def receipt(path, value):
    temporary = path.with_name('.' + uuid4().hex + '.tmp')
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--expected-qq-container-id', required=True)
    parser.add_argument('--expected-qq-image-id', required=True)
    parser.add_argument('--expected-platform-image-id', required=True)
    parser.add_argument('--execute', action='store_true', required=True)
    args = parser.parse_args()
    settings = HostSettings.model_validate_json(args.config.read_text(encoding='utf-8'))
    identity = uuid4()
    journal = HostJournal(settings.state_dir / 'operations')
    with journal.exclusive('platform'):
        journal.assert_available('platform', identity)
        stopped_qq(args.expected_qq_container_id, args.expected_qq_image_id)
        platform(args.expected_platform_image_id)
        directory = settings.state_dir / 'qq-unified-transfers' / str(identity)
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)
        record = {'operation_id': str(identity), 'state': 'preparing',
                  'qq_container_id': args.expected_qq_container_id,
                  'qq_image_id': args.expected_qq_image_id,
                  'platform_image_id': args.expected_platform_image_id}
        evidence = directory / 'transfer.json'
        receipt(evidence, record)
        importing = False
        try:
            data = snapshot(directory / 'legacy-snapshot.sqlite3')
            stopped_qq(args.expected_qq_container_id, args.expected_qq_image_id)
            platform(args.expected_platform_image_id)
            record['state'] = 'importing'
            receipt(evidence, record)
            importing = True
            result = subprocess.run(['docker', 'exec', '-i', 'al1s-platform-app', 'timeout',
                '--signal=TERM', '--kill-after=5s', '290s', 'python', '-m',
                'al1s.bot_ai.transfer_import', '--writers-stopped'], input=data, capture_output=True, timeout=310)
            if result.returncode:
                code = result.stderr.decode('utf-8', errors='replace').strip()
                raise ValueError(code if code.startswith('ai_') and len(code) < 100 else 'qq_transfer_import_failed')
            stopped_qq(args.expected_qq_container_id, args.expected_qq_image_id)
            platform(args.expected_platform_image_id)
            record.update(state='completed', result=json.loads(result.stdout))
            receipt(evidence, record)
            print(json.dumps({'state': 'completed', 'operation_id': str(identity),
                              'result': record['result']}, ensure_ascii=False))
        except BaseException as error:
            record['state'] = 'unknown' if importing else 'failed_before_import'
            record['error'] = str(error) if isinstance(error, ValueError) else 'qq_transfer_unconfirmed'
            receipt(evidence, record)
            raise


if __name__ == '__main__':
    main()
