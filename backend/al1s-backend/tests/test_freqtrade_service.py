from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.freqtrade.commands import dispatch, validate_words
from al1s.freqtrade.contracts import (
    DEFAULT_COMMANDS,
    FreqtradeCommand,
    FreqtradeRecord,
    FreqtradeSettings,
)
from al1s.freqtrade.service import FreqtradeService
from al1s.meme_workshop.contracts import CommandSource


def command(action="buy", arguments=("BTC", "100")):
    return FreqtradeCommand(
        source=CommandSource(
            message_id="1", scope_kind="qq_group", target_id="888", sender_id="123"
        ),
        action=action,
        arguments=arguments,
        event_timestamp=int(datetime.now(UTC).timestamp()),
        settings_version=1,
        mapping_version=1,
        normalized_keyword=DEFAULT_COMMANDS[action],
        settings=FreqtradeSettings(),
        commands=DEFAULT_COMMANDS,
    )


def test_replayed_event_never_repeats_native_order():
    repo, manager = MagicMock(), MagicMock()
    repo.claim.return_value = (SimpleNamespace(result="原请求已提交"), False)
    assert FreqtradeService(repo, manager).execute(uuid4(), command()) == "原请求已提交"
    manager.ensure.assert_not_called()


def test_uncertain_native_write_blocks_the_account_and_is_not_replayed():
    repo, manager = MagicMock(), MagicMock()
    receipt = SimpleNamespace(id=uuid4(), result=None)
    repo.claim.return_value = receipt, True
    profile = SimpleNamespace(id=uuid4(), state="ready", service_id=uuid4())
    repo.profile.return_value = profile
    repo.get.return_value = FreqtradeRecord()
    native = manager.ensure.return_value
    native.call.side_effect = [[], TimeoutError()]
    result = FreqtradeService(repo, manager).execute(uuid4(), command())
    assert "待核实" in result
    repo.set_profile.assert_called_once_with(profile.id, state="unknown")
    repo.complete.assert_called_once_with(receipt.id, result, unknown=True)
    assert native.call.call_args_list[-1].args[0] == "forceenter"


def test_owner_is_taken_from_onebot_and_scope_controls_access():
    record = FreqtradeRecord(
        row_version=1,
        settings=FreqtradeSettings(enabled=True, event_ws_url="ws://llbot:3001", groups=["888"]),
        commands=DEFAULT_COMMANDS,
    )
    event = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 999,
        "user_id": 123,
        "group_id": 888,
        "message_id": 1,
        "time": int(datetime.now(UTC).timestamp()),
        "message": [{"type": "text", "data": {"text": "币买入 BTC 100"}}],
    }
    result = dispatch(record, event)
    assert result.source.sender_id == "123" and result.arguments == ("BTC", "100")
    event["group_id"] = 777
    assert dispatch(record, event) is None
    event.update(message_type="private")
    assert dispatch(record, event) is None


def test_custom_words_must_not_conflict_with_other_features():
    import pytest

    with pytest.raises(ValueError, match="conflict"):
        validate_words({**DEFAULT_COMMANDS, "buy": "模拟买入"}, {"模拟买入"})


def test_help_uses_current_words_and_explains_distinct_amount_units():
    item = command("help", ())
    item.commands = {**DEFAULT_COMMANDS, "buy": "买币", "sell": "卖币"}
    result = FreqtradeService.help(item)
    assert "买币 BTC 100" in result and "100 USDT" in result
    assert "卖币 BTC 0.001" in result and "卖币 BTC 全部" in result
