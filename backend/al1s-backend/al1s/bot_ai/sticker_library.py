import asyncio
import hashlib
import io
import json
import logging
import os
import re
import uuid
from pathlib import Path

from PIL import Image

from al1s.bot_ai.input_media import _read
from al1s.bot_ai.managed.vision_images import prepare_images
from al1s.bot_ai.sticker_core.db.database_service import DatabaseService
from al1s.bot_ai.sticker_core.util.normalization import normalize_label_list

logger = logging.getLogger(__name__)


def read_capacity(path):
    import yaml

    path = Path(path)
    if not path.exists():
        return 500
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, yaml.YAMLError):
        raise ValueError('ai_sticker_config_invalid') from None
    value = data.get('max_reg_num', 500) if isinstance(data, dict) else None
    if type(value) is not int or not 1 <= value <= 100000:
        raise ValueError('ai_sticker_config_invalid')
    return value


class StickerRetryLater(Exception):
    pass


class StickerLibrary:
    def __init__(self, root, media_root, *, capacity):
        self.root, self.media_root = Path(root).resolve(), Path(media_root).resolve()
        if not self.root.exists():
            from al1s.bot_ai.sticker_core.default_categories import CATEGORIES, INFO

            self.root.mkdir(parents=True, mode=0o700)
            (self.root / 'categories.json').write_text(json.dumps(CATEGORIES, ensure_ascii=False), encoding='utf-8')
            (self.root / 'category_info.json').write_text(json.dumps(INFO, ensure_ascii=False), encoding='utf-8')
        self.categories_root = self.root / 'categories'
        self.capacity = capacity
        self.db = DatabaseService(self.root / 'cache' / 'emoji.db')
        self._lock = asyncio.Lock()
        self._processor = self._authorize = self._worker = None
        self._recover_file_operation()
        self._jobs = self._load_jobs()

    @property
    def pending(self):
        return len(self._jobs)

    def _load_jobs(self):
        raw = self.db.get_meta_value('al1s_import_jobs')
        if not raw:
            return []
        jobs = json.loads(raw)
        if not isinstance(jobs, list) or len(jobs) > 32:
            raise ValueError('ai_sticker_queue_invalid')
        staging = (self.root / 'staging' / 'al1s-import').resolve()
        for job in jobs:
            if (not isinstance(job, dict) or set(job) != {'id', 'service', 'group', 'provider', 'hash', 'file'}
                    or not re.fullmatch(r'[a-f0-9]{32}', str(job['id']))
                    or not re.fullmatch(r'[1-9][0-9]{0,19}', str(job['group']))
                    or not re.fullmatch(r'[a-f0-9]{64}', str(job['hash']))
                    or not isinstance(job['provider'], str) or not 0 < len(job['provider']) <= 128):
                raise ValueError('ai_sticker_queue_invalid')
            uuid.UUID(job['service'])
            file = Path(job['file'])
            if file.is_symlink() or file.resolve().parent != staging or file.name != job['id'] + '.image':
                raise ValueError('ai_sticker_queue_invalid')
        return jobs

    def _save_jobs(self):
        self.db.set_meta_value('al1s_import_jobs', json.dumps(self._jobs, ensure_ascii=False))

    def start_imports(self, processor, authorize=None):
        self._processor, self._authorize = processor, authorize
        self._ensure_worker()

    def _ensure_worker(self):
        if self._processor and self._jobs and (self._worker is None or self._worker.done()):
            self._worker = asyncio.create_task(self._process_imports())

    async def drain_imports(self):
        if self._worker:
            await asyncio.shield(self._worker)

    async def close(self):
        if self._worker and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None

    def _recover_file_operation(self):
        raw = self.db.get_meta_value('al1s_file_operation')
        if not raw:
            return
        try:
            item = json.loads(raw)
            operation = item['operation']
            source, target = Path(item['source']), Path(item['target'])
        except (ValueError, KeyError, TypeError):
            raise ValueError('ai_sticker_recovery_invalid') from None
        root = self.categories_root.resolve()
        if (operation not in {'move', 'delete'} or source == target
                or source.is_symlink() or target.is_symlink()
                or not source.resolve().is_relative_to(root) or not target.resolve().is_relative_to(root)
                or (operation == 'delete' and (source.parent != target.parent
                                               or not target.name.startswith('.deleting-')))):
            raise ValueError('ai_sticker_recovery_invalid')
        old_row, new_row = self.db.get_emoji(str(source)), self.db.get_emoji(str(target))
        old_file, new_file = source.is_file(), target.is_file()
        if old_row and not new_row and old_file and not new_file:
            pass
        elif old_row and not new_row and not old_file and new_file:
            target.rename(source)
        elif not old_row and operation == 'move' and new_row and not old_file and new_file:
            pass
        elif not old_row and operation == 'delete' and not new_row and not old_file:
            if new_file:
                target.unlink()
        else:
            raise ValueError('ai_sticker_recovery_ambiguous')
        self.db.set_meta_value('al1s_file_operation', '')

    def _journal(self, operation, source, target):
        self.db.set_meta_value('al1s_file_operation', json.dumps({
            'operation': operation, 'source': str(source), 'target': str(target)}))

    def _categories(self):
        categories = json.loads((self.root / 'categories.json').read_text(encoding='utf-8-sig'))
        info = json.loads((self.root / 'category_info.json').read_text(encoding='utf-8-sig'))
        if (not isinstance(categories, list) or not isinstance(info, dict)
                or any(not isinstance(key, str) or not key or '/' in key or '\\' in key or key in {'.', '..'}
                       for key in categories)):
            raise ValueError('ai_sticker_categories_invalid')
        return [{'key': key, 'name': str(info.get(key, {}).get('name') or key)} for key in categories]

    def _path(self, value):
        path = Path(value).resolve()
        if not path.is_relative_to(self.categories_root.resolve()) or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('ai_sticker_path_invalid')
        return path

    @staticmethod
    def _inherit_owner(path, parent):
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            owner = parent.stat()
            os.chown(path, owner.st_uid, owner.st_gid)

    def _category_directory(self, category):
        root = self.categories_root
        if not root.exists():
            root.mkdir()
            self._inherit_owner(root, self.root)
        directory = root / category
        if directory.is_symlink() or directory.resolve().parent != root.resolve():
            raise ValueError('ai_sticker_path_invalid')
        if not directory.exists():
            directory.mkdir()
            self._inherit_owner(directory, root)
        return directory

    async def _entries(self, service, group, *, enabled=True):
        index = await asyncio.to_thread(self.db.get_index_cache_readonly)
        blocked = await asyncio.to_thread(self.db.blacklisted_hashes)
        entries = {path: meta for path, meta in index.items() if isinstance(meta, dict)
            and meta.get('source') == 'al1s:' + str(service) and meta.get('scope_mode') == 'local'
            and meta.get('origin_target') == 'group:' + group
            and (not enabled or meta.get('hash') not in blocked)}
        return entries, blocked

    async def search(self, service, group, query, *, limit=5):
        text = str(query or '').strip().casefold()[:500]
        compact = re.sub(r'\s+', '', text)
        if not text or re.search(r'(?:不要|别|不用|不必|不想|停止|禁止).{0,8}(?:表情包|表情|动图)', compact):
            return []
        if not 1 <= limit <= 5:
            raise ValueError('ai_sticker_search_limit')
        async with self._lock:
            await asyncio.to_thread(self._recover_file_operation)
            entries, _ = await self._entries(service, group)
            return await asyncio.to_thread(self._rank, text, entries, limit)

    def allowed_now(self, service, group, identity):
        entry = self.db.get_emoji_by_hash(identity)
        if not entry or self.db.is_blacklisted(identity):
            return False
        path, meta = entry
        if (meta.get('source') != 'al1s:' + str(service) or meta.get('scope_mode') != 'local'
                or meta.get('origin_target') != 'group:' + group):
            return False
        try:
            self._path(path)
            return True
        except (OSError, ValueError):
            return False

    async def allowed(self, service, group, identity):
        async with self._lock:
            return await asyncio.to_thread(self.allowed_now, service, group, identity)

    async def record_sent(self, service, group, identity):
        async with self._lock:
            entry = await asyncio.to_thread(self.db.get_emoji_by_hash, identity)
            if not entry:
                return
            path, meta = entry
            if (meta.get('source') == 'al1s:' + str(service) and meta.get('scope_mode') == 'local'
                    and meta.get('origin_target') == 'group:' + group):
                await asyncio.to_thread(self.db.increment_usage_sync, path)

    async def stage(self, service, group, identity, request_id, candidates):
        if identity not in candidates:
            raise ValueError('ai_sticker_candidate_required')
        async with self._lock:
            task = asyncio.create_task(self._stage(service, group, identity, request_id))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def _stage(self, service, group, identity, request_id):
        if not await asyncio.to_thread(self.allowed_now, service, group, identity):
            raise ValueError('ai_sticker_unavailable')
        entry = await asyncio.to_thread(self.db.get_emoji_by_hash, identity)
        source = self._path(entry[0])
        directory = self.media_root / str(request_id)
        if directory.is_symlink() or directory.resolve().parent != self.media_root or not directory.is_dir():
            raise ValueError('ai_sticker_path_invalid')
        target = directory / ('sticker' + source.suffix[:12])
        body = await asyncio.to_thread(source.read_bytes)
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o640)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(body)
        return target

    def _rank(self, query, entries, limit):
        from al1s.bot_ai.sticker_core.search.managed_rank import rank_entries

        valid = {}
        for path, meta in entries.items():
            try:
                self._path(path)
            except (OSError, ValueError):
                continue
            valid[path] = meta
        info_path = self.root / 'character_info.json'
        info = json.loads(info_path.read_text(encoding='utf-8-sig')) if info_path.is_file() else {}
        ranked = [valid[path] for path in rank_entries(query, valid, info, limit)]
        return [{'id': meta['hash'], 'index': index + 1, 'description': str(meta.get('desc') or '')[:500],
                 'emotion': str(meta.get('category') or '')[:80], 'overlay_text': str(meta.get('overlay_text') or '')[:80],
                 'tags': list(meta.get('tags') or [])[:12], 'scenes': list(meta.get('scenes') or [])[:8]}
                for index, meta in enumerate(ranked)]

    async def control(self, service, command):
        async with self._lock:
            await asyncio.to_thread(self._recover_file_operation)
            task = asyncio.create_task(self._control(service, command))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def _control(self, service, command):
        if command.action == 'import':
            return await self._enqueue(service, command)
        entries, blocked = await self._entries(service, command.group_id, enabled=False)
        if command.action in {'list', 'status'}:
            categories = self._categories()
            labels = {item['key']: item['name'] for item in categories}
            ordered = sorted(entries.values(), key=lambda meta: (-int(meta.get('created_at') or 0),
                                                                  meta.get('hash') or ''))
            rows = [{'id': meta.get('hash'), 'description': str(meta.get('desc') or '')[:160],
                'category': str(meta.get('category') or '')[:80], 'enabled': meta.get('hash') not in blocked,
                'category_label': str(labels.get(meta.get('category'), meta.get('category') or ''))[:80],
                'overlay_text': str(meta.get('overlay_text') or '')[:80],
                'tags': [str(tag)[:40] for tag in (meta.get('tags') or [])[:3]],
                'scene_count': len(meta.get('scenes') or []), 'uses': int(meta.get('use_count') or 0),
                'group_id': command.group_id} for meta in ordered]
            return {'status': 'ready', 'total': len(rows), 'library_total': await asyncio.to_thread(self.db.count_total),
                    'capacity': self.capacity, 'pending': self.pending, 'categories': categories,
                    'items': rows[command.offset:command.offset + command.limit] if command.action == 'list' else []}
        source = next((path for path, meta in entries.items() if meta.get('hash') == command.image_id), None)
        if source is None:
            raise ValueError('ai_sticker_not_found')
        meta = entries[source]
        if command.action == 'detail':
            return {'status': 'ready', 'item': {'id': meta.get('hash'), 'category': meta.get('category', ''),
                'description': meta.get('desc') or '', 'overlay_text': meta.get('overlay_text') or '',
                'tags': list(meta.get('tags') or []), 'scenes': list(meta.get('scenes') or []),
                'uses': int(meta.get('use_count') or 0), 'group_id': command.group_id,
                'enabled': command.image_id not in blocked}}
        if command.action == 'preview':
            return {'media_ref': str(self._path(source))}
        if command.action == 'edit':
            await self._edit(source, meta, command)
        elif command.action == 'enable':
            await self.db.remove_blacklist(command.image_id)
        elif command.action == 'disable':
            await self.db.add_blacklist(command.image_id)
        elif command.action == 'delete':
            await self._delete(source)
        else:
            raise ValueError('ai_sticker_command_invalid')
        return {'status': 'ok'}

    async def _edit(self, source, meta, command):
        if command.category not in {item['key'] for item in self._categories()}:
            raise ValueError('ai_sticker_category_invalid')
        updates = {'desc': command.description, 'overlay_text': command.overlay_text,
                   'tags': command.tags, 'scenes': command.scenes}
        if meta.get('category') == command.category:
            if not await self.db.update_path(source, updates):
                raise ValueError('ai_sticker_not_found')
            return
        path = self._path(source)
        directory = self._category_directory(command.category)
        target = directory / path.name
        if target.exists():
            target = directory / (uuid.uuid4().hex + path.suffix)
        await asyncio.to_thread(self._journal, 'move', path, target)
        await asyncio.to_thread(path.rename, target)
        try:
            if not await self.db.move_path(source, str(target), command.category, updates):
                raise ValueError('ai_sticker_not_found')
        except BaseException:
            await asyncio.to_thread(target.rename, path)
            await asyncio.to_thread(self.db.set_meta_value, 'al1s_file_operation', '')
            raise
        await asyncio.to_thread(self.db.set_meta_value, 'al1s_file_operation', '')

    async def _delete(self, source):
        path = self._path(source)
        target = path.with_name('.deleting-' + uuid.uuid4().hex + path.suffix)
        await asyncio.to_thread(self._journal, 'delete', path, target)
        await asyncio.to_thread(path.rename, target)
        try:
            if await self.db.delete_paths([source]) != 1:
                raise ValueError('ai_sticker_not_found')
        except BaseException:
            await asyncio.to_thread(target.rename, path)
            await asyncio.to_thread(self.db.set_meta_value, 'al1s_file_operation', '')
            raise
        await asyncio.to_thread(target.unlink)
        await asyncio.to_thread(self.db.set_meta_value, 'al1s_file_operation', '')

    async def _enqueue(self, service, command):
        if self._processor is None:
            raise ValueError('ai_sticker_unavailable')
        total = await asyncio.to_thread(self.db.count_total)
        if total >= self.capacity:
            return {'status': 'full'}
        if len(self._jobs) >= 32 or total + len(self._jobs) >= self.capacity:
            return {'status': 'busy'}
        body = await asyncio.to_thread(_read, self.media_root, command.request_id, command.media_ref)
        await asyncio.to_thread(self._image_info, body)
        raw_hash = hashlib.sha256(body).hexdigest()
        scoped = hashlib.sha256(('al1s:' + str(service) + '\0group:' + command.group_id + '\0' + raw_hash).encode()).hexdigest()
        if await asyncio.to_thread(self.db.hash_exists, scoped) or any(job['hash'] == scoped for job in self._jobs):
            return {'status': 'queued'}
        staging = self.root / 'staging' / 'al1s-import'
        if staging.is_symlink() or not staging.resolve().is_relative_to(self.root):
            raise ValueError('ai_sticker_path_invalid')
        staging.mkdir(parents=True, exist_ok=True)
        identity = uuid.uuid4().hex
        target = staging / (identity + '.image')
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            self._jobs.append({'id': identity, 'service': str(service), 'group': command.group_id,
                               'provider': command.vision_provider_id, 'hash': scoped, 'file': str(target)})
            await asyncio.to_thread(self._save_jobs)
        except BaseException:
            self._jobs = [job for job in self._jobs if job['id'] != identity]
            target.unlink(missing_ok=True)
            raise
        self._ensure_worker()
        return {'status': 'queued'}

    @staticmethod
    def _image_info(body):
        with Image.open(io.BytesIO(body), formats=['PNG', 'JPEG', 'GIF', 'WEBP']) as image:
            if max(image.size) > 4096 or image.width * image.height > 8_000_000 or getattr(image, 'n_frames', 1) > 500:
                raise ValueError('ai_sticker_image_invalid')
            phash = ''
            if not getattr(image, 'is_animated', False):
                pixels = list(image.convert('L').resize((17, 16), Image.Resampling.BILINEAR).getdata())
                value = 0
                for row in range(16):
                    for col in range(16):
                        value = (value << 1) | int(pixels[row * 17 + col] > pixels[row * 17 + col + 1])
                phash = format(value, '064x')
            return image.format, image.width, image.height, phash

    async def _process_imports(self):
        while True:
            async with self._lock:
                if not self._jobs:
                    return
                job = dict(self._jobs[0])
            try:
                if self._authorize and not await self._authorize(uuid.UUID(job['service']), job['group'], job['provider']):
                    raise ValueError('ai_sticker_scope_revoked')
                image, hint = await asyncio.to_thread(prepare_images, [job['file']], (Path(job['file']).parent,))
                metadata = await self._processor(uuid.UUID(job['service']), job['provider'], image, hint)
                async with self._lock:
                    task = asyncio.create_task(self._store_import(job, metadata))
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        await task
                        raise
            except asyncio.CancelledError:
                raise
            except StickerRetryLater:
                await asyncio.sleep(5)
                continue
            except Exception as error:
                logger.warning('Sticker import failed: %s', type(error).__name__)
            async with self._lock:
                self._jobs = [item for item in self._jobs if item['id'] != job['id']]
                await asyncio.to_thread(self._save_jobs)
                Path(job['file']).unlink(missing_ok=True)

    async def _store_import(self, job, metadata):
        if not isinstance(metadata, dict):
            raise ValueError('ai_sticker_classification_invalid')
        path = Path(job['file'])
        body = await asyncio.to_thread(path.read_bytes)
        actual = hashlib.sha256(('al1s:' + job['service'] + '\0group:' + job['group'] + '\0'
                                 + hashlib.sha256(body).hexdigest()).encode()).hexdigest()
        if actual != job['hash']:
            raise ValueError('ai_sticker_staging_invalid')
        if await asyncio.to_thread(self.db.hash_exists, actual):
            return
        image_format, width, height, phash = await asyncio.to_thread(self._image_info, body)
        entries, _ = await self._entries(uuid.UUID(job['service']), job['group'], enabled=False)
        if phash:
            for existing, meta in entries.items():
                other = meta.get('phash')
                if isinstance(other, str) and len(other) == len(phash):
                    try:
                        similar = (int(other, 16) ^ int(phash, 16)).bit_count() <= 20
                    except ValueError:
                        similar = False
                    if similar and Path(existing).is_file():
                        existing_body = await asyncio.to_thread(Path(existing).read_bytes)
                        _, _, _, actual_phash = await asyncio.to_thread(self._image_info, existing_body)
                        if actual_phash and (int(actual_phash, 16) ^ int(phash, 16)).bit_count() <= 20:
                            return
        if await asyncio.to_thread(self.db.count_total) >= self.capacity:
            raise ValueError('ai_sticker_full')
        categories = [item['key'] for item in self._categories()]
        category = metadata.get('category')
        if category not in categories:
            category = 'confused' if 'confused' in categories else categories[0]
        extension = {'PNG': '.png', 'JPEG': '.jpg', 'GIF': '.gif', 'WEBP': '.webp'}[image_format]
        directory = self._category_directory(category)
        target = directory / (actual + extension)
        if target.exists():
            if target.is_symlink() or hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(body).digest():
                raise ValueError('ai_sticker_path_conflict')
        else:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o640)
            with os.fdopen(descriptor, 'wb') as output:
                if hasattr(os, 'geteuid') and os.geteuid() == 0:
                    owner = directory.stat()
                    os.fchown(output.fileno(), owner.st_uid, owner.st_gid)
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
        entry = {'path': str(target), 'hash': actual, 'phash': phash or None, 'category': category,
                 'desc': str(metadata.get('description') or '表情')[:80],
                 'overlay_text': str(metadata.get('overlay_text') or '')[:80],
                 'tags': normalize_label_list(metadata.get('tags') or [], 3),
                 'scenes': normalize_label_list(metadata.get('scenes') or [], 2),
                 'source': 'al1s:' + job['service'], 'origin_target': 'group:' + job['group'],
                 'scope_mode': 'local', 'width': width, 'height': height, 'format': image_format,
                 'bytes': len(body), 'add_method': 'manual'}
        if await self.db.insert_batch([entry]) != 1:
            raise ValueError('ai_sticker_store_failed')
