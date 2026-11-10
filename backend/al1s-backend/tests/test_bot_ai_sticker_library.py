import asyncio
import json
from uuid import uuid4

import pytest

from al1s.bot_ai.managed.smart_contract import StickerCommand, StickerImportCommand
from al1s.bot_ai.sticker_library import StickerLibrary, read_capacity


def test_scope_metadata_disable_edit_and_delete_preserve_other_groups(tmp_path):
    async def run():
        root = tmp_path / 'library'
        root.mkdir()
        (root / 'categories.json').write_text(json.dumps(['happy', 'sad']), encoding='utf-8')
        (root / 'category_info.json').write_text(json.dumps({'happy': {'name': '开心'},
                                                            'sad': {'name': '难过'}}), encoding='utf-8')
        service = uuid4()
        categories = root / 'categories' / 'happy'
        categories.mkdir(parents=True)
        first, other = categories / 'first.gif', categories / 'other.png'
        first.write_bytes(b'GIF89a')
        other.write_bytes(b'PNG-fixture')
        library = StickerLibrary(root, tmp_path / 'media', capacity=500)
        assert await library.db.insert_batch([
            {'path': str(first), 'hash': 'a' * 64, 'category': 'happy', 'desc': '开心',
             'tags': ['开心'], 'scenes': ['打招呼'], 'source': 'al1s:' + str(service),
             'scope_mode': 'local', 'origin_target': 'group:1', 'created_at': 5},
            {'path': str(other), 'hash': 'b' * 64, 'category': 'happy', 'desc': '别的群', 'tags': ['测试'],
             'source': 'al1s:' + str(service), 'scope_mode': 'local',
             'origin_target': 'group:2', 'created_at': 6},
        ]) == 2
        listing = await library.control(service, StickerCommand(action='list', group_id='1'))
        assert listing['total'] == 1 and listing['library_total'] == 2 and listing['capacity'] == 500
        assert listing['items'][0]['id'] == 'a' * 64
        assert [item['id'] for item in await library.search(service, '1', '请发开心表情')] == ['a' * 64]
        assert await library.search(service, '1', '不要开心表情') == []
        assert [item['id'] for item in await library.search(service, '2', '测试')] == ['b' * 64]
        request = uuid4()
        (tmp_path / 'media' / str(request)).mkdir(parents=True)
        staged = await library.stage(service, '1', 'a' * 64, request, {'a' * 64})
        assert staged.read_bytes() == b'GIF89a' and await library.allowed(service, '1', 'a' * 64)
        await library.record_sent(service, '1', 'a' * 64)
        counted = await library.control(service, StickerCommand(action='detail', group_id='1', image_id='a' * 64))
        assert counted['item']['uses'] == 1
        with pytest.raises(ValueError, match='candidate'):
            await library.stage(service, '1', 'b' * 64, request, {'a' * 64})
        with pytest.raises(ValueError, match='not_found'):
            await library.control(service, StickerCommand(action='detail', group_id='1', image_id='b' * 64))
        await library.control(service, StickerCommand(action='disable', group_id='1', image_id='a' * 64))
        assert not await library.allowed(service, '1', 'a' * 64)
        assert await library.search(service, '1', '开心') == []
        detail = await library.control(service, StickerCommand(action='detail', group_id='1', image_id='a' * 64))
        assert detail['item']['enabled'] is False
        await library.control(service, StickerCommand(action='edit', group_id='1', image_id='a' * 64,
            category='sad', description='新的描述', overlay_text='文字', tags=['新'], scenes=['安慰']))
        detail = await library.control(service, StickerCommand(action='detail', group_id='1', image_id='a' * 64))
        assert detail['item']['description'] == '新的描述' and detail['item']['category'] == 'sad'
        preview = await library.control(service, StickerCommand(action='preview', group_id='1', image_id='a' * 64))
        from pathlib import Path
        assert Path(preview['media_ref']).read_bytes() == b'GIF89a'
        await library.control(service, StickerCommand(action='delete', group_id='1', image_id='a' * 64))
        assert not Path(preview['media_ref']).exists() and other.exists() and library.db.count_total() == 1
    asyncio.run(run())


def test_incomplete_file_move_is_recovered_before_library_use(tmp_path):
    async def run():
        root = tmp_path / 'library'
        root.mkdir()
        (root / 'categories.json').write_text('["happy", "sad"]', encoding='utf-8')
        (root / 'category_info.json').write_text('{}', encoding='utf-8')
        happy, sad = root / 'categories' / 'happy', root / 'categories' / 'sad'
        happy.mkdir(parents=True)
        sad.mkdir()
        source, target = happy / 'one.gif', sad / 'one.gif'
        source.write_bytes(b'GIF89a')
        library = StickerLibrary(root, tmp_path / 'media', capacity=500)
        await library.db.insert_batch([{'path': str(source), 'hash': 'a' * 64, 'category': 'happy'}])
        library.db.set_meta_value('al1s_file_operation', json.dumps({
            'operation': 'move', 'source': str(source), 'target': str(target)}))
        source.rename(target)
        recovered = StickerLibrary(root, tmp_path / 'media', capacity=500)
        assert source.read_bytes() == b'GIF89a' and not target.exists()
        assert recovered.db.get_emoji(str(source)) is not None
        assert not recovered.db.get_meta_value('al1s_file_operation')
    asyncio.run(run())


def test_incomplete_delete_does_not_resurrect_database_entry(tmp_path):
    async def run():
        root = tmp_path / 'library'
        root.mkdir()
        (root / 'categories.json').write_text('["happy"]', encoding='utf-8')
        (root / 'category_info.json').write_text('{}', encoding='utf-8')
        directory = root / 'categories' / 'happy'
        directory.mkdir(parents=True)
        source, target = directory / 'one.gif', directory / '.deleting-test.gif'
        source.write_bytes(b'GIF89a')
        library = StickerLibrary(root, tmp_path / 'media', capacity=500)
        await library.db.insert_batch([{'path': str(source), 'hash': 'a' * 64, 'category': 'happy'}])
        library.db.set_meta_value('al1s_file_operation', json.dumps({
            'operation': 'delete', 'source': str(source), 'target': str(target)}))
        source.rename(target)
        await library.db.delete_paths([str(source)])
        recovered = StickerLibrary(root, tmp_path / 'media', capacity=500)
        assert not source.exists() and not target.exists() and recovered.db.count_total() == 0
        assert not recovered.db.get_meta_value('al1s_file_operation')
    asyncio.run(run())


def test_explicit_import_snapshots_original_and_resumes_after_restart(tmp_path):
    async def run():
        from PIL import Image

        root, media = tmp_path / 'library', tmp_path / 'media'
        root.mkdir()
        (root / 'categories.json').write_text('["happy"]', encoding='utf-8')
        (root / 'category_info.json').write_text('{}', encoding='utf-8')
        request, service = uuid4(), uuid4()
        source = media / str(request) / 'import.gif'
        source.parent.mkdir(parents=True)
        Image.new('RGB', (8, 8), 'red').save(source, 'GIF')
        started, gate = asyncio.Event(), asyncio.Event()
        async def classify(_service, _provider, image, _hint):
            assert image.startswith('data:image/')
            started.set()
            await gate.wait()
            return {'category': 'happy', 'description': '开心图片', 'tags': ['开心'], 'scenes': ['见面']}
        command = StickerImportCommand(group_id='1', request_id=request, media_ref=str(source),
                                       vision_provider_id='fixture')
        library = StickerLibrary(root, media, capacity=500)
        library.start_imports(classify)
        assert await library.control(service, command) == {'status': 'queued'}
        await asyncio.wait_for(started.wait(), 3)
        source.unlink()
        await library.close()
        assert library.pending == 1
        restored = StickerLibrary(root, media, capacity=500)
        restored.start_imports(classify)
        gate.set()
        await asyncio.wait_for(restored.drain_imports(), 3)
        listing = await restored.control(service, StickerCommand(action='list', group_id='1'))
        assert listing['total'] == 1 and listing['pending'] == 0
        item = listing['items'][0]
        assert item['description'] == '开心图片' and item['tags'] == ['开心']
        preview = await restored.control(service, StickerCommand(action='preview', group_id='1', image_id=item['id']))
        assert open(preview['media_ref'], 'rb').read(6) in (b'GIF87a', b'GIF89a')
        await restored.close()
    asyncio.run(run())


def test_new_library_has_default_categories_and_existing_capacity_is_preserved(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text('max_reg_num: 500\n', encoding='utf-8')
    assert read_capacity(config) == 500 and read_capacity(tmp_path / 'missing') == 500
    library = StickerLibrary(tmp_path / 'new-library', tmp_path / 'media', capacity=read_capacity(config))
    assert len(library._categories()) == 17


def test_preview_uses_the_configured_library_root(tmp_path):
    from PIL import Image
    from al1s.api.bot_ai import _sticker_preview

    root = tmp_path / 'custom-library' / 'categories'
    root.mkdir(parents=True)
    image = root / 'example.png'
    Image.new('RGB', (2, 2), 'red').save(image)
    body, mime = _sticker_preview(str(image), root)
    assert body == image.read_bytes() and mime == 'image/png'
    outside = tmp_path / 'outside.png'
    outside.write_bytes(body)
    with pytest.raises(ValueError, match='ai_sticker_unavailable'):
        _sticker_preview(str(outside), root)


def test_search_keeps_character_and_bm25_metadata(tmp_path):
    async def run():
        root = tmp_path / 'library'
        library = StickerLibrary(root, tmp_path / 'media', capacity=500)
        directory = library._category_directory('happy')
        first, second = directory / 'one.gif', directory / 'two.gif'
        first.write_bytes(b'GIF89a')
        second.write_bytes(b'GIF89a')
        service = uuid4()
        common = {'category': 'happy', 'source': 'al1s:' + str(service),
                  'scope_mode': 'local', 'origin_target': 'group:1'}
        await library.db.insert_batch([
            {**common, 'path': str(first), 'hash': 'a' * 64, 'character': '熊猫老师',
             'desc': '喝茶休息'},
            {**common, 'path': str(second), 'hash': 'b' * 64, 'desc': '早上问好'},
        ])
        assert (await library.search(service, '1', '熊猫老师'))[0]['id'] == 'a' * 64
        assert (await library.search(service, '1', '喝茶休息'))[0]['id'] == 'a' * 64
    asyncio.run(run())


def test_disabling_waits_for_accepted_sticker_imports_to_finish(tmp_path):
    async def run():
        from types import SimpleNamespace
        from PIL import Image
        from al1s.bot_ai.local_runtime import LocalAiRuntime
        from al1s.bot_ai.model_manager import LocalModelManager

        root, media, service = tmp_path / 'library', tmp_path / 'media', uuid4()
        library = StickerLibrary(root, media, capacity=500)
        settings = SimpleNamespace(enabled=True, desired_version=1, applied_version=1, groups=['1'], service_id=service)
        manager = LocalModelManager(LocalAiRuntime(), SimpleNamespace(get_settings=lambda _: settings),
                                    object(), media, stickers=library)
        manager._active[service] = SimpleNamespace(version=1, comfy=None)
        started, gate = asyncio.Event(), asyncio.Event()
        async def classify(*_):
            started.set()
            await gate.wait()
            return {'category': 'happy', 'description': 'fixture', 'tags': [], 'scenes': []}
        library.start_imports(classify, manager.authorize_import)
        for colors in [('red', 'blue'), ('green', 'yellow')]:
            request = uuid4()
            source = media / str(request) / 'import.gif'
            source.parent.mkdir(parents=True)
            first, second = (Image.new('RGB', (8, 8), color) for color in colors)
            first.save(source, 'GIF', save_all=True, append_images=[second], duration=100, loop=0)
            command = StickerImportCommand(group_id='1', request_id=request, media_ref=str(source),
                vision_provider_id=f'al1s_{service.hex}_1_vision')
            assert await library.control(service, command) == {'status': 'queued'}
        await asyncio.wait_for(started.wait(), 3)
        settings.enabled, settings.desired_version = False, 2
        with pytest.raises(ValueError, match='ai_sticker_queue_pending'):
            await manager.activate(settings, None, {})
        gate.set()
        await asyncio.wait_for(library.drain_imports(), 5)
        result = await library.control(service, StickerCommand(action='list', group_id='1'))
        assert result['total'] == 2 and result['pending'] == 0
        await manager.activate(settings, None, {})
        await manager.close()
    asyncio.run(run())
