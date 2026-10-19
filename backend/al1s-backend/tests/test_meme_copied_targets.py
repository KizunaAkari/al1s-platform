import asyncio
from io import BytesIO
from threading import Event
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from PIL import Image

from al1s.adapters.postgres.meme_settings import QqConnection
from al1s.meme_workshop.contracts import Layout, RenderedImage
from al1s.meme_workshop.onebot import parse_event
from al1s.meme_workshop.qq_worker import QqWorker
from al1s.meme_workshop.records import TemplateRecord
from al1s.meme_workshop.service import MemeWorkshopService


def event(text, *, private=False):
    return {
        "post_type": "message",
        "message_type": "private" if private else "group",
        "self_id": 100,
        "user_id": 200,
        "group_id": 300,
        "message_id": -123,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def test_copied_at_is_deferred_instead_of_silently_selecting_the_sender():
    message = parse_event(event("摸摸 @群名片\u00a0"), {"摸摸"})
    assert message is not None
    assert getattr(message, "copied_target_text", None) == "@群名片"
    assert message.avatar_ids == []


def test_copied_at_without_a_space_after_the_command_is_recognized():
    message = parse_event(event("摸摸@群名片"), {"摸摸"})
    assert message is not None and message.keyword == "摸摸"
    assert getattr(message, "copied_target_text", None) == "@群名片"


def test_real_at_precedes_plain_text_names_and_no_target_keeps_sender_default():
    value = event("摸摸 ")
    value["message"] += [
        {"type": "at", "data": {"qq": "400"}},
        {"type": "text", "data": {"text": " @群名片"}},
    ]
    message = parse_event(value, {"摸摸"})
    assert message.avatar_ids == ["400"] and getattr(message, "copied_target_text", None) is None
    assert parse_event(event("摸摸"), {"摸摸"}).avatar_ids == ["200"]


async def generation_case(monkeypatch, caption=False):
    keyword = "配字" if caption else "摸摸"
    raw = event(f"{keyword} @群名片")
    if caption:
        raw["message"].append(
            {"type": "image", "data": {"url": "https://gchat.qpic.cn/picture.png"}}
        )
    message = parse_event(raw, {keyword})
    workshop, client = MagicMock(), MagicMock()
    template = MagicMock(layout=Layout(engine="caption" if caption else "petpet"))
    workshop.repository.find_keyword.return_value = template
    workshop.render_command.return_value = RenderedImage(b"GIF", "image/gif", 64, 64)
    client.group_members.return_value = [{"user_id": 400, "nickname": "账号昵称", "card": "群名片"}]
    media = MagicMock(return_value=b"target-bytes")
    monkeypatch.setattr("al1s.meme_workshop.qq_worker.read_media", media)
    row = QqConnection(uuid4(), uuid4(), 1, "ws://gateway:3001", "http://gateway:3000", 5, None)
    worker = QqWorker(workshop, MagicMock())
    await worker.generate(row, client, message, uuid4())
    supplied = workshop.render_command.call_args.args[3]
    if caption:
        client.group_members.assert_not_called()
        assert supplied.texts == ["@群名片"]
    else:
        client.group_members.assert_called_once_with("300")
        media.assert_called_once_with("https://q1.qlogo.cn/g?b=qq&nk=400&s=640")
        assert supplied.avatars == [b"target-bytes"] and supplied.texts == []
    assert workshop.render_command.call_args.kwargs["selected_template"] is template


def test_copied_group_name_uses_the_resolved_qq_and_a_frozen_template(monkeypatch):
    asyncio.run(generation_case(monkeypatch))


def test_caption_preserves_at_text_without_group_member_queries(monkeypatch):
    asyncio.run(generation_case(monkeypatch, caption=True))


def test_avatar_composition_accepts_an_unspaced_copied_target_and_keeps_text():
    parsed = parse_event(event("头像合成 圆图@群名片 | 台词"), {"圆图"})
    assert parsed.keyword == "圆图" and parsed.copied_target_text == "@群名片"
    assert parsed.texts == ["@群名片", "台词"]


async def stopped_roster_case(monkeypatch):
    workshop, client = MagicMock(), MagicMock()
    workshop.repository.find_keyword.return_value = MagicMock(layout=Layout(engine="petpet"))
    started, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()

    def delayed_members(group):
        loop.call_soon_threadsafe(started.set)
        release.wait()
        return [{"user_id": 400, "nickname": "群名片", "card": ""}]

    client.group_members.side_effect = delayed_members
    media = MagicMock()
    monkeypatch.setattr("al1s.meme_workshop.qq_worker.read_media", media)
    worker = QqWorker(workshop, MagicMock())
    row = QqConnection(uuid4(), uuid4(), 1, "ws://gateway:3001", "http://gateway:3000", 5, None)
    task = asyncio.create_task(
        worker.generate(row, client, parse_event(event("摸摸 @群名片"), {"摸摸"}), uuid4())
    )
    await started.wait()
    await worker.stop()
    release.set()
    with pytest.raises(ValueError, match="meme_stopped"):
        await task
    media.assert_not_called()
    workshop.render_command.assert_not_called()


def test_a_late_roster_after_stop_never_starts_image_fetching(monkeypatch):
    asyncio.run(stopped_roster_case(monkeypatch))


@pytest.mark.parametrize(
    "roster,code",
    [
        ([], "copied_targets_not_found"),
        ([{"user_id": 200, "nickname": "群名片", "card": ""}], "copied_target_self_only"),
    ],
)
def test_unresolved_or_self_only_targets_reply_once_through_the_existing_gate(
    roster, code, monkeypatch
):
    async def run():
        workshop, client = MagicMock(), MagicMock()
        receipt = uuid4()
        workshop.repository.claim.return_value = receipt
        workshop.repository.start_send.return_value = True
        workshop.repository.find_keyword.return_value = MagicMock(layout=Layout(engine="petpet"))
        client.group_members.return_value = roster
        media = MagicMock()
        monkeypatch.setattr("al1s.meme_workshop.qq_worker.read_media", media)
        row = QqConnection(uuid4(), uuid4(), 1, "ws://gateway:3001", "http://gateway:3000", 5, None)
        worker = QqWorker(workshop, MagicMock())
        message = parse_event(event("摸摸 @群名片"), {"摸摸"})
        await worker.handle(row, client, message)
        media.assert_not_called()
        workshop.render_command.assert_not_called()
        workshop.repository.start_send.assert_called_once_with(
            row.service_id, receipt, error_code=code
        )
        client.send.assert_called_once()
        assert "蓝色@" in client.send.call_args.args[1]
        workshop.repository.settle.assert_called_once_with(row.service_id, receipt, "sent", code)

    asyncio.run(run())


def test_private_copied_name_does_not_read_a_group_roster():
    async def run():
        client = MagicMock()
        worker = QqWorker(MagicMock(), MagicMock())
        with pytest.raises(ValueError, match="group_only"):
            await worker.copied_targets(
                client, parse_event(event("摸摸 @群名片", private=True), {"摸摸"})
            )
        client.group_members.assert_not_called()

    asyncio.run(run())


def test_template_snapshot_is_leased_before_roster_lookup_and_is_not_requeried(monkeypatch):
    async def run():
        repository, client = MagicMock(), MagicMock()
        before = TemplateRecord(
            name="before",
            keyword="摸摸",
            layout=Layout(engine="petpet", width=64, height=64),
            id=uuid4(),
            version_id=uuid4(),
            version_no=1,
            row_version=1,
        )
        after = before.model_copy(
            update={"layout": Layout(engine="caption"), "version_id": uuid4()}
        )
        repository.find_keyword.return_value = before
        repository.receipt_available.return_value = True
        repository.bind_template.return_value = True
        repository.lease_version.return_value = True

        def change_template(group):
            repository.lease_version.assert_called_once_with(before.version_id)
            repository.find_keyword.return_value = after
            return [{"user_id": 400, "nickname": "群名片", "card": ""}]

        client.group_members.side_effect = change_template
        png = BytesIO()
        Image.new("RGBA", (64, 64), "red").save(png, "PNG")
        monkeypatch.setattr("al1s.meme_workshop.qq_worker.read_media", lambda url: png.getvalue())
        worker = QqWorker(MemeWorkshopService(repository, MagicMock()), MagicMock())
        row = QqConnection(uuid4(), uuid4(), 1, "ws://gateway:3001", "http://gateway:3000", 5, None)
        receipt = uuid4()
        result = await worker.generate(
            row, client, parse_event(event("摸摸 @群名片"), {"摸摸"}), receipt
        )
        assert result.media_type == "image/gif" and result.width == 64
        repository.find_keyword.assert_called_once_with("摸摸")
        assert repository.bind_template.call_count == 2
        assert all(
            call.args == (row.service_id, receipt, before.version_id)
            for call in repository.bind_template.call_args_list
        )
        assert repository.lease_version.call_count == 2

    asyncio.run(run())
