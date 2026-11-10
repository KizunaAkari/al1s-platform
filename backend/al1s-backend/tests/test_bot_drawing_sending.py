import asyncio
import hashlib
import threading
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

from PIL import Image

from al1s.bot_ai.contracts import AiEnvelope
from al1s.bot_ai.sender import AiSender
from al1s.meme_workshop.contracts import CommandSource


class Repository:
    def __init__(self): self.started, self.transport, self.finished, self.allowed_now = [], [], [], True
    def allowed(self, *_): return self.allowed_now
    def start_send(self, request, kind): self.started.append((request, kind)); return True
    def mark_transport_started(self, request, kind, sha256):
        self.transport.append((request, kind, sha256))
        return self.allowed_now
    def finish_send(self, request, kind, status, count, receipt=None): self.finished.append((request, kind, status, count, receipt))


class Client:
    def __init__(self): self.parts, self.quotes = [], []
    def send_segments(self, _source, parts, **kwargs):
        self.parts.extend(parts); self.quotes.append(kwargs.get('quote', True)); return '123'


def fixture(root):
    request = uuid4()
    folder = root / str(request)
    folder.mkdir()
    image = folder / 'output.png'
    Image.new('RGB', (8, 8), 'blue').save(image)
    envelope = AiEnvelope(request_id=request, service_id=uuid4(), config_version=1,
        source=CommandSource(message_id='1', scope_kind='qq_group', target_id='100', sender_id='200'),
        self_id='300', sender_name='测试', parts=[{'type': 'text', 'text': '绘图'}],
        deadline=datetime.now(UTC) + timedelta(seconds=30))
    return envelope, image


def test_guard_checks_the_actual_prepared_bytes_and_mismatch_never_sends(tmp_path):
    async def run():
        envelope, image = fixture(tmp_path)
        base, drawing, client = Repository(), Repository(), Client()
        sender = AiSender(base, tmp_path, interval=0)
        expected = hashlib.sha256(b'different').hexdigest()
        result = await sender.send(envelope, 0, [{'type': 'image', 'media_ref': str(image)}], client,
            receipt_repository=drawing, receipt_kind='result', media_root=tmp_path,
            prepared_image_guard=lambda _reference, digest: digest == expected)
        assert result['code'] == 'audit_recheck_required' and client.parts == [] and drawing.transport == []
        assert base.started == []
    asyncio.run(run())


def test_final_transport_check_can_revoke_send_after_audit(tmp_path):
    async def run():
        envelope, image = fixture(tmp_path)
        repo, client = Repository(), Client()
        sender = AiSender(Repository(), tmp_path, interval=0)
        def changed(_reference, _digest):
            repo.allowed_now = False
            return True
        result = await sender.send(envelope, 0, [{'type': 'image', 'media_ref': str(image)}], client,
            receipt_repository=repo, receipt_kind='result', media_root=tmp_path, prepared_image_guard=changed)
        assert result['code'] == 'audit_recheck_required' and client.parts == []
        assert repo.transport[0][2] == hashlib.sha256(image.read_bytes()).hexdigest()
    asyncio.run(run())


def test_notice_and_result_have_independent_receipts(tmp_path):
    async def run():
        envelope, image = fixture(tmp_path)
        repo, client = Repository(), Client()
        sender = AiSender(Repository(), tmp_path, interval=0)
        notice = await sender.send(envelope, 0, [{'type': 'text', 'text': '正在绘图'}], client,
                                   receipt_repository=repo, receipt_kind='notice')
        result = await sender.send(envelope, 0, [{'type': 'image', 'media_ref': str(image)}], client,
            receipt_repository=repo, receipt_kind='result', media_root=tmp_path,
            prepared_image_guard=lambda _ref, _hash: True)
        assert notice['status'] == result['status'] == 'sent'
        assert [row[1] for row in repo.finished] == ['notice', 'result']
        assert client.quotes == [False, True]
        assert all(row[4] == '123' for row in repo.finished)
    asyncio.run(run())


def test_queue_notice_has_no_onebot_reply_segment_but_result_does(tmp_path, monkeypatch):
    from al1s.meme_workshop.onebot import OneBotClient
    async def run():
        envelope, image = fixture(tmp_path)
        repo, sent = Repository(), []
        client = OneBotClient('http://gateway:3000', None)
        def post(method, payload):
            sent.append((method, payload))
            return {'message_id': 123}
        monkeypatch.setattr(client, 'post', post)
        sender = AiSender(Repository(), tmp_path, interval=0)
        await sender.send(envelope, 0, [{'type': 'text', 'text': '当前任务队列'}], client,
                          receipt_repository=repo, receipt_kind='notice')
        await sender.send(envelope, 0, [{'type': 'image', 'media_ref': str(image)}], client,
                          receipt_repository=repo, receipt_kind='result', media_root=tmp_path,
                          prepared_image_guard=lambda _ref, _digest: True)
        assert [part['type'] for part in sent[0][1]['message']] == ['text']
        assert [part['type'] for part in sent[1][1]['message']] == ['reply', 'image']
    asyncio.run(run())


def test_gif_send_preserves_all_frames_and_audited_bytes(tmp_path):
    async def run():
        envelope, png = fixture(tmp_path)
        output = BytesIO()
        frames = [Image.new('RGB', (8, 8), color) for color in ('red', 'green', 'blue')]
        frames[0].save(output, 'GIF', save_all=True, append_images=frames[1:], duration=200, loop=0)
        body = output.getvalue()
        gif = png.with_suffix('.gif')
        gif.write_bytes(body)
        repo, client = Repository(), Client()
        sender = AiSender(Repository(), tmp_path, interval=0)
        digest = hashlib.sha256(body).hexdigest()
        result = await sender.send(envelope, 0, [{'type': 'image', 'media_ref': str(gif)}], client,
            receipt_repository=repo, receipt_kind='result', media_root=tmp_path,
            prepared_image_guard=lambda _ref, actual: actual == digest)
        assert result['status'] == 'sent'
        assert client.parts[0]['body'] == body and repo.transport[0][2] == digest
        with Image.open(BytesIO(client.parts[0]['body'])) as received:
            assert received.n_frames == 3
    asyncio.run(run())


def test_timed_out_transport_remains_unknown_and_holds_shared_send_lock(tmp_path):
    async def run():
        envelope, _ = fixture(tmp_path)
        repo, started, release = Repository(), threading.Event(), threading.Event()
        class SlowClient:
            def send_segments(self, *_args, **_kwargs): started.set(); release.wait(2); return '123'
        sender = AiSender(Repository(), tmp_path, interval=0, send_timeout=0.02, queue_timeout=0.03)
        result = await sender.send(envelope, 0, [{'type': 'text', 'text': '排队中'}], SlowClient(),
                                   receipt_repository=repo, receipt_kind='notice')
        assert result['status'] == 'unknown' and started.is_set()
        assert sender._locks[envelope.service_id].locked()
        release.set()
        await asyncio.sleep(0.05)
        assert not sender._locks[envelope.service_id].locked()
        assert repo.finished[-1][2] == 'unknown'
    asyncio.run(run())
