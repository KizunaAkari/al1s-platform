import pytest

from al1s.quant.commands import DEFAULT_COMMANDS, parse_quant_event, validate_mappings


def event(text, *, sender=123456, timestamp=1790000000):
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": 999999,
        "user_id": sender,
        "group_id": 888888,
        "message_id": -42,
        "time": timestamp,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def test_custom_command_uses_sender_and_preserves_arguments():
    mapping = {**DEFAULT_COMMANDS, "buy": "买股票"}
    parsed = parse_quant_event(event("买股票 600000 100"), mapping, set(), 8)
    assert parsed.action == "buy" and parsed.arguments == ("600000", "100")
    assert parsed.source.sender_id == "123456" and parsed.source.target_id == "888888"
    assert parsed.mapping_version == 8 and parsed.event_timestamp == 1790000000
    assert parsed.normalized_keyword == "买股票"
    assert parse_quant_event(event("模拟买入 600000 100"), mapping, set(), 8) is None


def test_prefix_is_a_complete_keyword_and_bot_mention_is_supported():
    assert parse_quant_event(event("我想模拟买入 600000 100"), DEFAULT_COMMANDS, set(), 1) is None
    e = event("模拟买入 600000 100")
    e["message"].insert(0, {"type": "at", "data": {"qq": "999999"}})
    assert parse_quant_event(e, DEFAULT_COMMANDS, set(), 1).action == "buy"
    e["message"].insert(0, {"type": "text", "data": {"text": " "}})
    assert parse_quant_event(e, DEFAULT_COMMANDS, set(), 1).action == "buy"
    e["message"].insert(2, {"type": "at", "data": {"qq": "333333"}})
    assert parse_quant_event(e, DEFAULT_COMMANDS, set(), 1) is None


def test_conflicting_or_invalid_keywords_are_not_executable():
    with pytest.raises(ValueError, match="conflict"):
        validate_mappings({**DEFAULT_COMMANDS, "buy": "摸摸"}, {"摸摸"})
    with pytest.raises(ValueError):
        validate_mappings({**DEFAULT_COMMANDS, "buy": "有 空格"}, set())
    with pytest.raises(ValueError):
        validate_mappings({**DEFAULT_COMMANDS, "buy": " 买入 "}, set())
    with pytest.raises(ValueError, match="conflict"):
        validate_mappings({**DEFAULT_COMMANDS, "buy": "模拟卖出"}, set())
    assert (
        parse_quant_event(event("模拟买入 600000 100"), DEFAULT_COMMANDS, {"模拟买入"}, 2) is None
    )


def test_invalid_or_self_authored_event_is_ignored():
    assert parse_quant_event(event("模拟开户", sender=999999), DEFAULT_COMMANDS, set(), 1) is None
    assert parse_quant_event(event("模拟开户", timestamp=0), DEFAULT_COMMANDS, set(), 1) is None
