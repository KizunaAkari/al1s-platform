import asyncio

from al1s.bot_ai.sticker_core.db.database_service import DatabaseService


def test_existing_library_format_keeps_scope_metadata_and_blacklist(tmp_path):
    async def run():
        db = DatabaseService(tmp_path / 'cache' / 'emoji.db')
        first, second = str(tmp_path / 'one.gif'), str(tmp_path / 'two.png')
        entries = [
            {'path': first, 'hash': 'a' * 64, 'category': 'happy', 'desc': '开心',
             'tags': ['开心'], 'scenes': ['见面'], 'scope_mode': 'local',
             'source': 'al1s:service', 'origin_target': 'group:1'},
            {'path': second, 'hash': 'b' * 64, 'category': 'happy', 'desc': '别的群',
             'tags': ['测试'], 'scenes': [], 'scope_mode': 'local',
             'source': 'al1s:service', 'origin_target': 'group:2'},
        ]
        assert await db.insert_batch(entries) == 2
        index = db.get_index_cache_readonly()
        assert index[first]['origin_target'] == 'group:1' and index[first]['tags'] == ['开心']
        assert index[first]['scenes'] == ['见面'] and index[second]['origin_target'] == 'group:2'
        await db.add_blacklist('a' * 64)
        assert db.is_blacklisted('a' * 64) and not db.is_blacklisted('b' * 64)
        assert await db.update_path(first, {'desc': '新描述', 'tags': ['新标签']})
        assert db.get_emoji(first)['desc'] == '新描述'
        assert await db.delete_paths([first]) == 1
        assert db.count_total() == 1 and db.get_emoji(second)['desc'] == '别的群'
    asyncio.run(run())
