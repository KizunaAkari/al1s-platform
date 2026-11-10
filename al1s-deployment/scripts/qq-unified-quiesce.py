"""Pause and restore AI admission through the existing QQ settings API."""

import argparse
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import UUID, uuid4

from al1s.components.host.config import HostSettings
from al1s.components.host.journal import HostJournal

FIELDS = ('event_ws_url', 'allow_private', 'groups', 'wake_words', 'persona', 'vision_enabled',
          'stt_enabled', 'tts_mode', 'reply_all_groups', 'smart_reply_groups', 'smart_reply')
PROVIDER_FIELDS = ('adapter_kind', 'model_id', 'base_url', 'voice')


def helpers():
    spec = importlib.util.spec_from_file_location('qq_unified_transfer',
        Path(__file__).with_name('qq-unified-transfer.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def writable(record, enabled):
    result = {name: record[name] for name in FIELDS}
    result['enabled'] = enabled
    result['providers'] = {role: {name: provider[name] for name in PROVIDER_FIELDS}
                           for role, provider in record['providers'].items()}
    return result


def running_qq(helper, container_id, image_id):
    row = helper.inspect('al1s-bot-qq')
    labels = row['Config'].get('Labels') or {}
    if (row['Id'] != container_id or row['Image'] != image_id or not row['State']['Running']
            or row['State'].get('Paused') or labels.get('com.docker.compose.project') != 'al1s-bot'
            or labels.get('com.docker.compose.service') != 'llbot'):
        raise ValueError('ai_quiesce_writer_changed')


def worker(action, *, service=None, version=None, key=None, body=None):
    program = Path(__file__).with_name('qq-unified-settings-worker.py').read_text(encoding='utf-8')
    command = ['docker', 'exec', '-i', 'al1s-bot-qq', 'python', '-c', program, action]
    if service is not None:
        command.extend(['--service', str(UUID(service))])
    if version is not None:
        command.extend(['--version', str(version), '--key', key])
    result = subprocess.run(command, input=json.dumps(body, ensure_ascii=False).encode() if body is not None else b'',
                            capture_output=True, timeout=30)
    if result.returncode:
        error = result.stderr.decode('utf-8', errors='replace').strip()
        raise ValueError(error if error.startswith('ai_') and len(error) < 100 else 'ai_quiesce_unconfirmed')
    return json.loads(result.stdout)


def wait_applied(service, version, enabled):
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        current = worker('read', service=service)
        if current['desired_version'] != version or current['enabled'] != enabled:
            raise ValueError('ai_quiesce_settings_changed')
        if current.get('last_error') and current['last_error'] not in {
                'ai_sticker_queue_pending', 'ai_vision_cleanup_pending'}:
            raise ValueError('ai_quiesce_apply_failed')
        if current['applied_version'] == version:
            return
        time.sleep(1)
    raise ValueError('ai_quiesce_apply_timeout')


def wait_drained(helper, container_id, image_id, *, closed):
    stable_after, deadline = time.monotonic() + 15, time.monotonic() + 180
    while time.monotonic() < deadline:
        running_qq(helper, container_id, image_id)
        inventory = worker('inventory')
        if inventory['pending'] or (closed and inventory['enabled']):
            raise ValueError('ai_quiesce_settings_changed')
        if not inventory['active'] and not inventory['staging'] and time.monotonic() >= stable_after:
            return
        if inventory['active'] or inventory['staging']:
            stable_after = time.monotonic() + 15
        time.sleep(1)
    raise ValueError('ai_quiesce_drain_timeout')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['pause', 'resume'])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--expected-qq-container-id', required=True)
    parser.add_argument('--expected-qq-image-id', required=True)
    parser.add_argument('--state-id')
    parser.add_argument('--execute', action='store_true', required=True)
    args = parser.parse_args()
    settings = HostSettings.model_validate_json(args.config.read_text(encoding='utf-8'))
    helper = helpers()
    identity = UUID(args.state_id) if args.state_id else uuid4()
    if args.action == 'resume' and not args.state_id:
        raise ValueError('ai_quiesce_state_required')
    directory = settings.state_dir / 'qq-unified-admission' / str(identity)
    evidence = directory / 'settings.json'
    journal = HostJournal(settings.state_dir / 'operations')
    with journal.exclusive('platform'):
        journal.assert_available('platform', identity)
        running_qq(helper, args.expected_qq_container_id, args.expected_qq_image_id)
        if args.action == 'pause':
            directory.mkdir(parents=True, mode=0o700)
            os.chmod(directory, 0o700)
            record = {'id': str(identity), 'state': 'pausing', 'services': {}}
            helper.receipt(evidence, record)
            try:
                wait_drained(helper, args.expected_qq_container_id, args.expected_qq_image_id, closed=False)
                while True:
                    inventory = worker('inventory')
                    if inventory['pending']:
                        raise ValueError('ai_quiesce_settings_pending')
                    if not inventory['enabled']:
                        break
                    for service in inventory['enabled']:
                        if service in record['services']:
                            raise ValueError('ai_quiesce_settings_changed')
                        current = worker('read', service=service)
                        if not current['enabled'] or current['desired_version'] != current['applied_version']:
                            raise ValueError('ai_quiesce_settings_pending')
                        item = {'original': writable(current, True), 'initial_version': current['desired_version'],
                                'disable_key': 'qq-unified-disable-' + uuid4().hex, 'state': 'disabling'}
                        record['services'][service] = item
                        helper.receipt(evidence, record)
                        saved = worker('write', service=service, version=item['initial_version'],
                                       key=item['disable_key'], body=writable(current, False))
                        if saved['enabled'] or saved['desired_version'] != item['initial_version'] + 1:
                            raise ValueError('ai_quiesce_write_unconfirmed')
                        item['disabled_version'] = saved['desired_version']
                        helper.receipt(evidence, record)
                        wait_applied(service, item['disabled_version'], False)
                        item['state'] = 'disabled'
                        helper.receipt(evidence, record)
                wait_drained(helper, args.expected_qq_container_id, args.expected_qq_image_id, closed=True)
                record['state'] = 'paused'
                helper.receipt(evidence, record)
            except BaseException:
                record['state'] = 'needs_review'
                helper.receipt(evidence, record)
                raise
        else:
            if not evidence.is_file() or evidence.stat().st_size > 8 * 1024 * 1024:
                raise ValueError('ai_quiesce_state_invalid')
            record = json.loads(evidence.read_text(encoding='utf-8'))
            if record.get('id') != str(identity) or record.get('state') not in {'paused', 'needs_review', 'resuming'}:
                raise ValueError('ai_quiesce_state_invalid')
            record['state'] = 'resuming'
            helper.receipt(evidence, record)
            try:
                for service, item in record['services'].items():
                    if item['state'] == 'restored':
                        continue
                    if item['state'] != 'disabled' or 'disabled_version' not in item:
                        raise ValueError('ai_quiesce_outcome_unknown')
                    current = worker('read', service=service)
                    if (current['enabled'] or current['desired_version'] != item['disabled_version']
                            or current['applied_version'] != item['disabled_version']
                            or writable(current, True) != item['original']):
                        raise ValueError('ai_quiesce_settings_changed')
                    item['state'] = 'resuming'
                    item['resume_key'] = 'qq-unified-resume-' + uuid4().hex
                    helper.receipt(evidence, record)
                    saved = worker('write', service=service, version=item['disabled_version'],
                                   key=item['resume_key'], body=item['original'])
                    if not saved['enabled'] or saved['desired_version'] != item['disabled_version'] + 1:
                        raise ValueError('ai_quiesce_write_unconfirmed')
                    wait_applied(service, saved['desired_version'], True)
                    item['state'] = 'restored'
                    helper.receipt(evidence, record)
                record['state'] = 'resumed'
                helper.receipt(evidence, record)
            except BaseException:
                record['state'] = 'needs_review'
                helper.receipt(evidence, record)
                raise
        print(json.dumps({'state': record['state'], 'state_id': str(identity),
                          'services': len(record['services'])}))


if __name__ == '__main__':
    main()
