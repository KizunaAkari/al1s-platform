"""Run inside the existing ops container with the unified QQ writer stopped."""

import argparse
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4
import sqlite3

from al1s.components.host.config import HostSettings
from al1s.components.host.journal import HostJournal

STICKER_DB = '/var/lib/al1s-astrbot/data/plugin_data/astrbot_plugin_stealer/cache/emoji.db'


def check_sticker_state(data):
    database = sqlite3.connect(':memory:')
    try:
        database.deserialize(data)
        rows = dict(database.execute("select key,value from meta where key in "
                                     "('al1s_import_jobs','al1s_file_operation')"))
        if rows.get('al1s_file_operation'):
            raise ValueError('qq_restore_sticker_recovery_pending')
        jobs = json.loads(rows.get('al1s_import_jobs') or '[]')
        if not isinstance(jobs, list) or jobs:
            raise ValueError('qq_restore_sticker_import_pending')
    finally:
        database.close()


def transfer_helpers():
    path = Path(__file__).with_name('qq-unified-transfer.py')
    spec = importlib.util.spec_from_file_location('qq_unified_transfer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_archive(directory):
    subprocess.run(['docker', 'cp', 'al1s-bot-qq:/opt/al1s/al1s', str(directory / 'al1s')],
                   check=True, capture_output=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in (directory / 'al1s').rglob('*.py'):
            if '__pycache__' not in path.parts:
                archive.write(path, path.relative_to(directory).as_posix())
    data = output.getvalue()
    if not data or len(data) > 32 * 1024 * 1024:
        raise ValueError('qq_restore_source_invalid')
    return data


def compose_args(settings):
    registration = settings.bots.registrations['qq']
    command = ['docker', 'compose', '-p', 'al1s-bot']
    for path in [*registration.env_files, settings.bots.selectors_file]:
        command.extend(['--env-file', str(path)])
    for path in registration.compose_files:
        command.extend(['-f', str(path)])
    return command


def cleanup_worker(name, identity, image_id):
    result = subprocess.run(['docker', 'inspect', name], capture_output=True)
    if result.returncode:
        if b'No such object' in result.stderr or b'No such container' in result.stderr:
            return 'absent'
        raise ValueError('qq_restore_worker_cleanup_unconfirmed')
    rows = json.loads(result.stdout)
    row = rows[0] if len(rows) == 1 else None
    labels = (row.get('Config', {}).get('Labels') or {}) if row else {}
    if (not row or row['Name'].removeprefix('/') != name or row['Image'] != image_id
            or labels.get('com.docker.compose.project') != 'al1s-bot'
            or labels.get('com.docker.compose.service') != 'llbot'
            or labels.get('al1s.qq-unified-restore') != str(identity)):
        raise ValueError('qq_restore_worker_identity_changed')
    if row['State']['Running']:
        subprocess.run(['docker', 'stop', '--time', '30', row['Id']], capture_output=True, check=True, timeout=45)
        return 'stopped'
    removed = subprocess.run(['docker', 'rm', row['Id']], capture_output=True, timeout=20)
    if removed.returncode and b'No such container' not in removed.stderr:
        raise ValueError('qq_restore_worker_cleanup_unconfirmed')
    return 'removed'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--expected-qq-container-id', required=True)
    parser.add_argument('--expected-qq-image-id', required=True)
    parser.add_argument('--expected-platform-image-id', required=True)
    parser.add_argument('--legacy-image-id', required=True)
    parser.add_argument('--execute', action='store_true', required=True)
    args = parser.parse_args()
    settings = HostSettings.model_validate_json(args.config.read_text(encoding='utf-8'))
    if settings.bots is None:
        raise ValueError('qq_restore_registration_missing')
    helpers = transfer_helpers()
    identity = uuid4()
    journal = HostJournal(settings.state_dir / 'operations')
    with journal.exclusive('platform'):
        journal.assert_available('platform', identity)
        helpers.stopped_qq(args.expected_qq_container_id, args.expected_qq_image_id)
        helpers.platform(args.expected_platform_image_id)
        images = json.loads(subprocess.check_output(['docker', 'image', 'inspect', args.legacy_image_id]))
        if len(images) != 1 or images[0]['Id'] != args.legacy_image_id:
            raise ValueError('qq_restore_legacy_image_invalid')
        command = compose_args(settings)
        environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'DOCKER_HOST', 'DOCKER_CONFIG')
                       if key in os.environ}
        environment['AL1S_QQ_BOT_IMAGE'] = args.legacy_image_id
        config = json.loads(subprocess.check_output([*command, 'config', '--format', 'json'], env=environment))
        qq = config['services']['llbot']
        mounts = qq.get('volumes', [])
        observed = helpers.inspect('al1s-bot-qq')
        observed_mounts = observed.get('Mounts', [])
        observed_env = dict(value.split('=', 1) for value in observed['Config'].get('Env', []) if '=' in value)
        environment_matches = all(str(value or '') == observed_env.get(key, '')
                                  for key, value in qq.get('environment', {}).items())
        if (config.get('name') != 'al1s-bot' or qq.get('container_name') != 'al1s-bot-qq'
                or qq.get('image') != args.legacy_image_id or len(mounts) != 1
                or mounts[0].get('type') != 'bind' or mounts[0].get('target') != '/app/llbot/data'
                or len(observed_mounts) != 1 or mounts[0].get('source') != observed_mounts[0].get('Source')
                or not observed_mounts[0].get('RW') or not environment_matches):
            raise ValueError('qq_restore_compose_changed')
        directory = settings.state_dir / 'qq-unified-transfers' / str(identity)
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)
        record = {'operation_id': str(identity), 'state': 'preparing',
                  'qq_container_id': args.expected_qq_container_id,
                  'qq_image_id': args.expected_qq_image_id,
                  'platform_image_id': args.expected_platform_image_id,
                  'legacy_image_id': args.legacy_image_id}
        evidence = directory / 'restore.json'
        helpers.receipt(evidence, record)
        restoring = False
        worker_name = 'al1s-qq-unified-restore-' + identity.hex[:16]
        try:
            helpers.snapshot(directory / 'legacy-before-restore.sqlite3')
            sticker = helpers.snapshot(directory / 'sticker-before-restore.sqlite3', source_path=STICKER_DB)
            check_sticker_state(sticker)
            with tempfile.TemporaryDirectory(prefix='al1s-qq-restore-source-') as temporary:
                source = source_archive(Path(temporary))
            helpers.stopped_qq(args.expected_qq_container_id, args.expected_qq_image_id)
            helpers.platform(args.expected_platform_image_id)
            worker = Path(__file__).with_name('qq-unified-restore-worker.py').read_text(encoding='utf-8')
            record['state'] = 'restoring'
            record['worker_name'] = worker_name
            helpers.receipt(evidence, record)
            restoring = True
            result = subprocess.run([*command, 'run', '--rm', '--no-deps', '-T', '--pull', 'never',
                '--name', worker_name, '--label', 'al1s.qq-unified-restore=' + str(identity),
                '--user', '993:993', '--entrypoint', '/opt/al1s-astrbot-venv/bin/python', 'llbot',
                '-c', worker, '--sqlite', '/var/lib/al1s-astrbot/data/data_v4.db',
                '--legacy-root', '/var/lib/al1s-astrbot', '--writers-stopped'],
                input=source, capture_output=True, timeout=300, env=environment)
            if cleanup_worker(worker_name, identity, args.legacy_image_id) == 'stopped':
                raise ValueError('qq_restore_worker_unconfirmed')
            if result.returncode:
                code = result.stderr.decode('utf-8', errors='replace').strip()
                raise ValueError(code if code.startswith('ai_') and len(code) < 100 else 'qq_restore_worker_failed')
            helpers.stopped_qq(args.expected_qq_container_id, args.expected_qq_image_id)
            helpers.platform(args.expected_platform_image_id)
            record.update(state='completed', result=json.loads(result.stdout))
            helpers.receipt(evidence, record)
            print(json.dumps({'state': 'completed', 'operation_id': str(identity), 'result': record['result']}))
        except BaseException as error:
            record['state'] = 'unknown' if restoring else 'failed_before_restore'
            record['error'] = str(error) if isinstance(error, ValueError) else 'qq_restore_unconfirmed'
            if restoring:
                try:
                    record['worker_cleanup'] = cleanup_worker(worker_name, identity, args.legacy_image_id)
                except Exception:
                    record['worker_cleanup'] = 'unconfirmed'
            helpers.receipt(evidence, record)
            raise


if __name__ == '__main__':
    main()
