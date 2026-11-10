import asyncio
import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from al1s.bot_ai.contracts import AiEnvelope
from al1s.bot_ai.input_media import build_user_message
from al1s.meme_workshop.contracts import CommandSource


def event(request, parts):
    return AiEnvelope(request_id=request, service_id=uuid4(), config_version=1,
        source=CommandSource(message_id='1', scope_kind='qq_group', target_id='100', sender_id='200'),
        self_id='300', sender_name='小明', mentions=('400',), parts=parts,
        deadline=datetime.now(UTC) + timedelta(seconds=30))


def test_images_audio_and_real_mention_are_preserved_in_current_turn(tmp_path):
    async def run():
        request = uuid4()
        directory = tmp_path / str(request)
        directory.mkdir()
        image, audio = directory / 'input.png', directory / 'input.wav'
        image.write_bytes(b'png')
        audio.write_bytes(b'wav')
        async def transcribe(value):
            assert value == b'wav'
            return '语音内容'
        message = await build_user_message(event(request, [
            {'type': 'text', 'text': '你好'}, {'type': 'image', 'media_ref': str(image)},
            {'type': 'record', 'media_ref': str(audio)}]), tmp_path, transcribe=transcribe)
        assert '[发言者 小明]' in message['content'][0]['text']
        assert 'mention1' in message['content'][0]['text'] and '400' not in message['content'][0]['text']
        url = next(part['image_url']['url'] for part in message['content'] if part['type'] == 'image_url')
        assert base64.b64decode(url.split(',', 1)[1]) == b'png'
        assert any('语音内容' in part.get('text', '') for part in message['content'])
    asyncio.run(run())


def test_media_outside_own_request_is_rejected(tmp_path):
    async def run():
        request = uuid4()
        (tmp_path / str(request)).mkdir()
        other = tmp_path / 'other.png'
        other.write_bytes(b'png')
        with pytest.raises(ValueError, match='media'):
            await build_user_message(event(request, [{'type':'image', 'media_ref':str(other)}]), tmp_path)
    asyncio.run(run())


def test_separate_vision_returns_description_instead_of_pretending_image_was_seen(tmp_path):
    async def run():
        request = uuid4()
        directory = tmp_path / str(request)
        directory.mkdir()
        image = directory / 'input.png'
        image.write_bytes(b'png')
        async def describe(url, hint):
            assert url.startswith('data:image/png;base64,') and hint == '截图'
            return '图片里有一只猫'
        message = await build_user_message(event(request, [{'type':'image','media_ref':str(image),
                        'caption_hint':'截图'}]), tmp_path, describe=describe)
        assert any('图片里有一只猫' in part.get('text', '') for part in message['content'])
        assert all(part['type'] == 'text' for part in message['content'])
    asyncio.run(run())
