import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from PIL import Image

from al1s.bot_ai.contracts import AiEnvelope
from al1s.bot_ai.sender import AiSender
from al1s.meme_workshop.contracts import CommandSource


class Repository:
    def allowed(self, *_args): return True
    def start_send(self, *_args): return True
    def finish_send(self, *_args): pass


class Client:
    def __init__(self): self.parts = []
    def send_segments(self, _source, parts, **_kwargs): self.parts.extend(parts)


def fixture(tmp_path):
    request = uuid4()
    folder = tmp_path / str(request)
    folder.mkdir()
    image = folder / 'sticker.png'
    Image.new('RGB', (8, 8), 'red').save(image)
    envelope = AiEnvelope(request_id=request, service_id=uuid4(), config_version=1,
        source=CommandSource(message_id='1', scope_kind='qq_group', target_id='100', sender_id='200'),
        self_id='300', sender_name='测试', parts=[{'type': 'text', 'text': 'hi'}],
        deadline=datetime.now(UTC) + timedelta(seconds=30))
    return envelope, image


def test_disabled_sticker_is_skipped_but_text_is_preserved(tmp_path):
    async def run():
        envelope, image = fixture(tmp_path)
        client = Client()
        sender = AiSender(Repository(), tmp_path, interval=0)
        result = await sender.send(envelope, 0, [{'type':'text','text':'hello'},
            {'type':'image','media_ref':str(image)}], client, image_allowed=lambda _ref: False)
        assert result['status'] == 'sent' and result['sent_media'] == []
        assert [part['type'] for part in client.parts] == ['text']
    asyncio.run(run())


def test_disabled_sticker_only_reply_falls_back_to_text(tmp_path):
    async def run():
        envelope, image = fixture(tmp_path)
        client = Client()
        sender = AiSender(Repository(), tmp_path, interval=0)
        result = await sender.send(envelope, 0, [{'type':'image','media_ref':str(image)}], client,
                                   image_allowed=lambda _ref: False, fallback_text='hello')
        assert result['status'] == 'sent' and result['sent_media'] == []
        assert client.parts == [{'type':'text','text':'hello'}]
    asyncio.run(run())


def test_cancellation_during_sticker_validation_prevents_send(tmp_path):
    async def run():
        envelope, image = fixture(tmp_path)
        client, active = Client(), [True]
        sender = AiSender(Repository(), tmp_path, interval=0)
        def guard(_ref):
            active[0] = False
            return True
        result = await sender.send(envelope, 0, [{'type':'image','media_ref':str(image)}], client,
                                   image_allowed=guard, is_active=lambda: active[0])
        assert result['status'] == 'failed' and client.parts == []
    asyncio.run(run())
