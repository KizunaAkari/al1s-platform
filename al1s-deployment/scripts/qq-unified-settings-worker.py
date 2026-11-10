"""One-shot internal access to the existing authenticated QQ settings API."""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID


def call(method, service, body=None, *, version=None, key=None):
    identity = str(UUID(service))
    headers = {'X-AL1S-Meme-Control': os.environ['AL1S_MEME_BACKEND_TOKEN']}
    if body is not None:
        headers.update({'Content-Type': 'application/json', 'If-Match': str(version), 'Idempotency-Key': key})
    request = Request('http://127.0.0.1:3101/api/v1/bots/ai/settings/' + identity,
                      data=body, headers=headers, method=method)
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
            data = response.read(65537)
    except HTTPError as error:
        raise ValueError('ai_quiesce_http_' + str(error.code)) from None
    if len(data) > 65536:
        raise ValueError('ai_quiesce_response_oversized')
    return json.loads(data)


def inventory():
    from sqlalchemy import create_engine, func, select
    from al1s.adapters.postgres.bot_ai_models import BotAiRequestRow, BotAiSettingsRow

    engine = create_engine(os.environ['AL1S_MEME_DATABASE_URL'])
    try:
        with engine.connect() as connection:
            enabled = connection.scalars(select(BotAiSettingsRow.service_id)
                .where(BotAiSettingsRow.enabled.is_(True)).order_by(BotAiSettingsRow.service_id).limit(101)).all()
            pending = connection.scalar(select(func.count()).select_from(BotAiSettingsRow).where(
                BotAiSettingsRow.desired_version != BotAiSettingsRow.applied_version))
            active = connection.scalar(select(func.count()).select_from(BotAiRequestRow).where(
                BotAiRequestRow.status.in_(['queued', 'running', 'stopping', 'blocked'])))
        if len(enabled) > 100:
            raise ValueError('ai_quiesce_service_limit')
        staging = Path('/var/lib/al1s-astrbot/data/plugin_data/astrbot_plugin_stealer/staging')
        files = sum(1 for item in staging.rglob('*') if item.is_file()) if staging.is_dir() else 0
        return {'enabled': [str(item) for item in enabled], 'pending': pending, 'active': active, 'staging': files}
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['inventory', 'read', 'write'])
    parser.add_argument('--service')
    parser.add_argument('--version', type=int)
    parser.add_argument('--key')
    args = parser.parse_args()
    if args.action == 'inventory':
        result = inventory()
    elif args.action == 'read':
        result = call('GET', args.service)
    else:
        body = sys.stdin.buffer.read(65537)
        if len(body) > 65536 or args.version is None or not args.key:
            raise ValueError('ai_quiesce_request_invalid')
        saved = call('PUT', args.service, body, version=args.version, key=args.key)
        result = {'enabled': saved['enabled'], 'desired_version': saved['desired_version'],
                  'applied_version': saved['applied_version']}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        code = str(error)
        print(code if isinstance(error, ValueError) and code.startswith('ai_') else 'ai_quiesce_failed', file=sys.stderr)
        raise SystemExit(1)
