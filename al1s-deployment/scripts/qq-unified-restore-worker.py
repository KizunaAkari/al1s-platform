"""One-shot legacy history restoration; run only with all QQ writers stopped."""

import argparse
import asyncio
import io
import json
import os
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

MAX_SOURCE_BYTES = 32 * 1024 * 1024


async def restore(database_path, legacy_root, archive_data):
    if not archive_data or len(archive_data) > MAX_SOURCE_BYTES:
        raise ValueError('ai_restore_source_invalid')
    os.environ['ASTRBOT_ROOT'] = legacy_root
    os.environ['HOME'] = legacy_root
    os.environ['XDG_CACHE_HOME'] = str(Path(legacy_root) / 'cache')
    with tempfile.TemporaryDirectory(prefix='al1s-qq-restore-') as temporary:
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            files = archive.infolist()
            if sum(item.file_size for item in files) > MAX_SOURCE_BYTES or any(
                    PurePosixPath(item.filename).is_absolute() or '..' in PurePosixPath(item.filename).parts
                    for item in files):
                raise ValueError('ai_restore_source_invalid')
            archive.extractall(temporary)
        sys.path.insert(0, temporary)
        sys.path.insert(1, '/opt/al1s-astrbot-src')
        sys.path.append('/opt/al1s-venv/lib/python3.12/site-packages')

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from al1s.adapters.postgres.bot_ai_history import AiHistoryRepository
        from al1s.bot_ai.legacy_restore import restore_legacy_history, restore_legacy_vision_jobs
        from astrbot.core.db.sqlite import SQLiteDatabase
        from astrbot.core.utils.shared_preferences import SharedPreferences
        import astrbot.core as core
        import astrbot.core.conversation_mgr as manager_module

        engine = create_engine(os.environ['AL1S_MEME_DATABASE_URL'])
        database = sqlite3.connect(database_path)
        helper = SQLiteDatabase(database_path)
        preferences = None
        try:
            repository = AiHistoryRepository(sessionmaker(engine, expire_on_commit=False))
            services, endpoints = repository.migration_sources()
            await helper.initialize()
            helper.inited = True
            preferences = SharedPreferences(helper)
            core.sp = preferences
            manager_module.sp = preferences
            manager = manager_module.ConversationManager(helper)
            counts = []
            for service in sorted(services, key=str):
                counts.append(await restore_legacy_history(database, manager, repository, service,
                                                           is_quiesced=lambda: True))
            vision = restore_legacy_vision_jobs(database, repository, endpoints, is_quiesced=lambda: True)
            return {'conversations': sum(item['conversations'] for item in counts), 'vision': vision}
        finally:
            if preferences is not None:
                await preferences.close()
            await helper.engine.dispose()
            database.close()
            engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sqlite', required=True)
    parser.add_argument('--legacy-root', required=True)
    parser.add_argument('--writers-stopped', action='store_true', required=True)
    args = parser.parse_args()
    path = Path(args.sqlite)
    if not path.is_file() or path.is_symlink() or not Path(args.legacy_root).is_dir():
        raise ValueError('ai_restore_database_invalid')
    print(json.dumps(asyncio.run(restore(str(path), args.legacy_root, sys.stdin.buffer.read(MAX_SOURCE_BYTES + 1)))))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        code = str(error)
        print(code if isinstance(error, ValueError) and code.startswith('ai_') else 'ai_restore_failed',
              file=sys.stderr)
        raise SystemExit(1)
