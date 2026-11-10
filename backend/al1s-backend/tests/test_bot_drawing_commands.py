import asyncio
import time
from io import BytesIO

import pytest
from PIL import Image

from al1s.bot_drawing import commands
from al1s.bot_drawing.commands import parse_drawing_event, resolve_reference


def event(*segments, private=False):
    return {"post_type": "message", "message_type": "private" if private else "group", "self_id": 100,
        "user_id": 200, "group_id": 300, "message_id": -123, "time": int(time.time()), "message": list(segments)}


def text(value):
    return {"type": "text", "data": {"text": value}}


def at(value="100"):
    return {"type": "at", "data": {"qq": value}}


def image(value="https://example.test/image.png"):
    return {"type": "image", "data": {"url": value}}


def reply(value="456"):
    return {"type": "reply", "data": {"id": value}}


def picture(format="PNG", *, animated=False):
    output = BytesIO()
    first = Image.new("RGB", (8, 8), "red")
    if animated:
        first.save(output, format, save_all=True, append_images=[Image.new("RGB", (8, 8), "blue")])
    else:
        first.save(output, format)
    return output.getvalue()


class Client:
    def __init__(self, result):
        self.result = result

    def post(self, action, payload):
        assert action == "get_msg" and payload == {"message_id": 456}
        return self.result


def group_message(sender=100, segments=None, group=300):
    return {"message_type": "group", "group_id": group, "sender": {"user_id": sender},
            "message": [image()] if segments is None else segments}


@pytest.mark.parametrize("segments,private", [
    ([at(), text("你好")], False),
    ([at(), text("画一只猫")], False),
    ([reply(), text("这个不错")], False),
    ([text("正面：blue hair")], True),
    ([text("我想了解 /wf3 画猫")], False),
])
def test_unprefixed_messages_never_enter_drawing(segments, private):
    assert parse_drawing_event(event(*segments, private=private), "100") is None


def test_workflow_number_and_prompt_are_in_the_same_message():
    command = parse_drawing_event(event(text("/wf3 画一只蓝猫")), "100")
    assert command is not None
    assert command.operation == "workflow_draw"
    assert command.workflow_number == 3 and command.prompt == "画一只蓝猫"
    assert command.image_reference is None and command.reference_message_id is None


@pytest.mark.parametrize("value,number", [("/gpt 画一只猫", None), ("/gpt12 画一只猫", 12)])
def test_online_command_selects_default_or_number(value, number):
    command = parse_drawing_event(event(text(value)), "100")
    assert command.operation == "online_draw"
    assert command.workflow_number == number and command.prompt == "画一只猫"
    assert parse_drawing_event(event(text("请解释 " + value)), "100") is None


def test_online_command_accepts_image_and_rejects_number_zero(monkeypatch):
    body = picture()
    monkeypatch.setattr(commands, "read_media", lambda _: body)
    command = parse_drawing_event(event(reply(), text("/gpt3 更换背景")), "100")
    assert asyncio.run(resolve_reference(command, Client(group_message(sender=200)))) == body
    with pytest.raises(commands.DrawingParseError):
        parse_drawing_event(event(text("/gpt0 画一只猫")), "100")


@pytest.mark.parametrize("value", ["/wf 画一只猫", "/wf0 画一只猫"])
def test_workflow_draw_requires_a_positive_number(value):
    with pytest.raises(commands.DrawingParseError, match="drawing_command_invalid"):
        parse_drawing_event(event(text(value)), "100")


def test_workflow_edit_accepts_same_group_member_image_without_at(monkeypatch):
    body = picture()
    monkeypatch.setattr(commands, "read_media", lambda _reference: body)
    command = parse_drawing_event(event(reply(), text("/wf5 把衣服改红")), "100")
    assert command is not None and command.operation == "workflow_draw"
    assert command.workflow_number == 5 and command.prompt == "把衣服改红"
    assert asyncio.run(resolve_reference(command, Client(group_message(sender=200)))) == body


def test_explicit_workflow_is_recognized_with_or_without_mention():
    assert parse_drawing_event(event(text("画一只猫")), "100") is None
    assert parse_drawing_event(event(text("@100 画一只猫")), "100") is None
    assert parse_drawing_event(event(at("999"), text("画一只猫")), "100") is None
    command = parse_drawing_event(event(at(), text("/wf3 画一只猫")), "100")
    assert command.operation == "workflow_draw" and command.mentioned and command.prompt == "画一只猫"
    assert parse_drawing_event(event(text("/wf3 画一只猫"), private=True), "100").operation == "workflow_draw"


@pytest.mark.parametrize("value,number", [("/wf", None), ("/wf0", 0), ("/wf2", 2)])
def test_workflow_commands_are_explicit(value, number):
    command = parse_drawing_event(event(text(value)), "100")
    assert command.operation == "workflow" and command.workflow_number == number


def test_tag_requires_reply_and_real_mention_in_group():
    assert parse_drawing_event(event(reply(), text("/tag")), "100") is None
    command = parse_drawing_event(event(reply(), at(), text("/tag")), "100")
    assert command.operation == "tag" and command.reference_message_id == "456"
    assert parse_drawing_event(event(reply(), text("/tag"), private=True), "100").operation == "tag"
    assert parse_drawing_event(event(at(), text("/tag")), "100").reference_message_id is None


def test_p2p_requires_reply_and_can_ask_for_missing_instruction():
    command = parse_drawing_event(event(reply(), text("/p2p 换成雪景")), "100")
    assert command.operation == "p2p" and command.prompt == "换成雪景"
    assert parse_drawing_event(event(reply(), text("/p2p")), "100").prompt == ""
    assert parse_drawing_event(event(text("/p2p 修改")), "100").reference_message_id is None


def test_only_current_message_text_is_used_for_commands():
    assert parse_drawing_event(event(reply(), text("这个不错")), "100") is None
    command = parse_drawing_event(event(reply(), text("/wf5 改成红色")), "100")
    assert command.operation == "workflow_draw" and command.prompt == "改成红色"
    assert parse_drawing_event(event(text("请问 /wf2 是什么")), "100") is None
    assert parse_drawing_event(event(text("普通聊天")), "100") is None


def test_p2p_accepts_single_image_from_any_sender_in_the_same_group(monkeypatch):
    body = picture()
    monkeypatch.setattr(commands, "read_media", lambda reference: body)
    command = parse_drawing_event(event(reply(), text("/p2p 换背景")), "100")
    assert asyncio.run(resolve_reference(command, Client(group_message()))) == body
    assert asyncio.run(resolve_reference(command, Client(group_message(sender=200)))) == body
    for message in (group_message(group=301), group_message(segments=[image(), image()]),
                    group_message(segments=[text("没图片")])):
        with pytest.raises(ValueError, match="drawing_image_"):
            asyncio.run(resolve_reference(command, Client(message)))


def test_tag_may_reference_another_group_member(monkeypatch):
    body = picture("JPEG")
    monkeypatch.setattr(commands, "read_media", lambda reference: body)
    command = parse_drawing_event(event(reply(), at(), text("/tag")), "100")
    assert asyncio.run(resolve_reference(command, Client(group_message(sender=200)))) == body


def test_explicit_workflow_reply_to_text_does_not_borrow_it_as_image():
    command = parse_drawing_event(event(reply(), text("/wf3 画一只猫")), "100")
    assert asyncio.run(resolve_reference(command, Client(group_message(segments=[text("上一条回复")])))) is None
    assert asyncio.run(resolve_reference(command, Client(group_message(sender=200, segments=[text("群友文字")])))) is None
    with pytest.raises(ValueError, match="drawing_image_scope_invalid"):
        asyncio.run(resolve_reference(command, Client(group_message(group=301))))


@pytest.mark.parametrize("body", [picture("GIF", animated=True), picture("WEBP", animated=True), b"not an image"])
def test_invalid_or_animated_images_are_rejected(monkeypatch, body):
    monkeypatch.setattr(commands, "read_media", lambda reference: body)
    command = parse_drawing_event(event(at(), text("/wf5 换背景"), image()), "100")
    with pytest.raises(ValueError, match="drawing_image_invalid"):
        asyncio.run(resolve_reference(command, Client(None)))


def test_ambiguous_images_and_invalid_event_are_not_accepted():
    with pytest.raises(ValueError, match="drawing_image_source_invalid"):
        parse_drawing_event(event(at(), text("/wf5 换背景"), image(), reply()), "100")
    with pytest.raises(ValueError, match="drawing_image_source_invalid"):
        parse_drawing_event(event(at(), text("/wf5 换背景"), image(), image()), "100")
    item = event(at(), text("/wf3 画猫"))
    assert parse_drawing_event(item, "999") is None
    item["user_id"] = 100
    assert parse_drawing_event(item, "100") is None
    item["user_id"] = 200
    item["time"] = int(time.time()) - 301
    assert parse_drawing_event(item, "100") is None
