"""One-shot import of a stopped QQ writer's SQLite backup from stdin."""

import argparse
import json
import sqlite3
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.bot_ai_history import AiHistoryRepository
from al1s.app.config import get_settings
from al1s.bot_ai.legacy_history import import_legacy_history, import_legacy_vision_jobs

MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024


def import_snapshot(data, repository):
    if not data or len(data) > MAX_SNAPSHOT_BYTES:
        raise ValueError('ai_transfer_snapshot_size_invalid')
    database = sqlite3.connect(':memory:')
    try:
        database.deserialize(data)
        database.execute('PRAGMA query_only=ON')
        if database.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise ValueError('ai_transfer_snapshot_invalid')
        services, endpoints = repository.migration_sources()
        history = import_legacy_history(database, repository, services)
        vision = import_legacy_vision_jobs(database, repository, endpoints)
        return {'history': history, 'vision': vision}
    finally:
        database.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--writers-stopped', action='store_true', required=True)
    parser.parse_args()
    data = sys.stdin.buffer.read(MAX_SNAPSHOT_BYTES + 1)
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as connection:
            revision = connection.scalar(text('select version_num from alembic_version'))
        if revision != '20261101_0070':
            raise ValueError('ai_transfer_schema_invalid')
        repository = AiHistoryRepository(sessionmaker(engine, expire_on_commit=False))
        print(json.dumps(import_snapshot(data, repository), ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        code = str(error)
        print(code if isinstance(error, ValueError) and code.startswith('ai_') else 'ai_transfer_failed',
              file=sys.stderr)
        raise SystemExit(1)
