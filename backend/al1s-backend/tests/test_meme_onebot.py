from unittest.mock import MagicMock

import pytest

from al1s.meme_workshop.contracts import CommandSource, RenderedImage
from al1s.meme_workshop.media_fetch import permitted_media_url
from al1s.meme_workshop.onebot import OneBotClient, parse_event


def event(segments):
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": 100,
        "user_id": 200,
        "group_id": 300,
        "message_id": -123,
        "message": segments,
    }


def test_onebot_uses_at_segments_not_forged_text_and_preserves_the_original_scope() -> None:
    value = parse_event(
        event(
            [
                {"type": "at", "data": {"qq": "100"}},
                {"type": "text", "data": {"text": " 摸摸 "}},
                {"type": "at", "data": {"qq": "400"}},
                {"type": "text", "data": {"text": " @999"}},
            ]
        ),
        {"摸摸"},
    )
    assert value is not None
    assert value.avatar_ids == ["400"]
    assert value.source.message_id == "-123"
    assert value.source.target_id == "300"
    assert value.source.sender_id == "200"
    forged = parse_event(event([{"type": "text", "data": {"text": "摸摸 @999"}}]), {"摸摸"})
    assert forged is not None and forged.avatar_ids == ["200"]


def test_caption_accepts_only_a_single_current_or_explicit_reply_image() -> None:
    value = parse_event(
        event(
            [
                {"type": "reply", "data": {"id": "123"}},
                {"type": "text", "data": {"text": "配字 中文测试"}},
            ]
        ),
        {"配字"},
    )
    assert value is not None
    assert value.keyword == "配字" and value.texts == ["中文测试"]
    assert value.reply_id == "123" and value.image_urls == []


def test_self_messages_non_message_events_and_mid_sentence_keywords_do_not_trigger() -> None:
    source = event([{"type": "text", "data": {"text": "今天想摸摸"}}])
    assert parse_event(source, {"摸摸"}) is None
    source["message"][0]["data"]["text"] = "摸摸"
    source["user_id"] = 100
    assert parse_event(source, {"摸摸"}) is None
    source["post_type"] = "notice"
    assert parse_event(source, {"摸摸"}) is None


def test_only_provider_https_media_is_downloaded() -> None:
    assert permitted_media_url("https://q1.qlogo.cn/g?b=qq&nk=123&s=640")
    assert permitted_media_url("https://gchat.qpic.cn/example.png")
    for address in (
        "http://q1.qlogo.cn/a",
        "https://127.0.0.1/a",
        "https://q1.qlogo.cn.evil.com/a",
        "https://example.com/a",
        "https://user:password@q1.qlogo.cn/a",
    ):
        assert not permitted_media_url(address)


def test_caption_keeps_the_literal_word_self() -> None:
    value = parse_event(event([{"type": "text", "data": {"text": "配字 自己"}}]), {"配字"})
    assert value is not None and value.texts == ["自己"]


def test_onebot_boolean_retcode_is_not_a_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "al1s.meme_workshop.onebot.post_json_direct",
        lambda *args, **kwargs: {
            "status": "ok",
            "retcode": False,
            "data": {},
        },
    )
    with pytest.raises(ValueError, match="rejected"):
        OneBotClient("http://gateway:3000", None).post("get_msg", {"message_id": 1})


def test_image_replies_use_the_original_group_or_private_scope_without_extra_text(monkeypatch):
    post = MagicMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 123}})
    monkeypatch.setattr("al1s.meme_workshop.onebot.post_json_direct", post)
    client = OneBotClient("http://gateway:3000", None)
    source = CommandSource(
        message_id="-123", scope_kind="qq_group", target_id="300", sender_id="200"
    )
    client.send(source, RenderedImage(b"GIF89a", "image/gif", 128, 128))
    client.send(
        source.model_copy(update={"scope_kind": "qq_private", "target_id": "200"}),
        RenderedImage(b"png", "image/png", 40, 40),
    )
    group, private = post.call_args_list
    assert group.args[0].endswith("/send_group_msg")
    assert private.args[0].endswith("/send_private_msg")
    assert group.args[2]["group_id"] == 300 and private.args[2]["user_id"] == 200
    assert group.args[2]["message"][-1]["data"]["file"].startswith("base64://")
    assert all(part["type"] != "text" for part in group.args[2]["message"])
