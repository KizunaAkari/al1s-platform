import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.freqtrade.contracts import (
    DEFAULT_COMMANDS,
    FreqtradeCommand,
    FreqtradeRecord,
    FreqtradeSettings,
)
from al1s.freqtrade.qq_adapter import FreqtradeQqAdapter
from al1s.meme_workshop.contracts import CommandSource


def event(message="币买入 BTC 100", *, group_id=888, message_type="group"):
    return {
        "post_type": "message",
        "message_type": message_type,
        "self_id": 999,
        "user_id": 123,
        "group_id": group_id,
        "message_id": "1",
        "time": 1_735_000_000,
        "message": [{"type": "text", "data": {"text": message}}],
    }


def configured_record(*, interval=10):
    return FreqtradeRecord(
        settings=FreqtradeSettings(
            enabled=True,
            event_ws_url="ws://llbot:3001",
            groups=["888"],
            reply_interval_seconds=interval,
        ),
        commands=dict(DEFAULT_COMMANDS),
        row_version=1,
    )


def command(*, action="buy", interval=10):
    settings = FreqtradeSettings(reply_interval_seconds=interval)
    return FreqtradeCommand(
        source=CommandSource(
            message_id="message-1", scope_kind="qq_group", target_id="888", sender_id="123"
        ),
        action=action,
        arguments=("BTC", "100"),
        event_timestamp=1_735_000_000,
        settings_version=1,
        mapping_version=1,
        normalized_keyword=DEFAULT_COMMANDS[action],
        settings=settings,
        commands=dict(DEFAULT_COMMANDS),
    )


def test_refresh_keeps_only_snapshots_acknowledged_by_native_client():
    service_a, service_b, stale = uuid4(), uuid4(), uuid4()
    rows = [
        SimpleNamespace(service_id=service_a, event_url="ws://a"),
        SimpleNamespace(service_id=service_b, event_url="ws://b"),
    ]
    repository, native, replies = MagicMock(), MagicMock(), MagicMock()
    repository.qq_connections.return_value = rows
    repository.snapshots.return_value = {
        service_a: configured_record(),
        service_b: configured_record(),
    }
    native.activate.side_effect = [1, 0]
    adapter = FreqtradeQqAdapter(repository, native, replies)
    adapter.records = {stale: configured_record()}

    accepted = asyncio.run(adapter.refresh())

    assert accepted == [rows[0]]
    assert adapter.records == {service_a: repository.snapshots.return_value[service_a]}
    assert native.activate.call_count == 2


def test_parse_enforces_event_endpoint_group_and_preserves_sender_identity():
    service_id = uuid4()
    row = SimpleNamespace(service_id=service_id, event_url="ws://llbot:3001")
    repository, native, replies = MagicMock(), MagicMock(), MagicMock()
    repository.keyword_conflicts.return_value = False
    adapter = FreqtradeQqAdapter(repository, native, replies)
    adapter.records[service_id] = configured_record()

    accepted = asyncio.run(adapter.parse(row, event()))
    wrong_endpoint = asyncio.run(
        adapter.parse(SimpleNamespace(service_id=service_id, event_url="ws://other"), event())
    )
    wrong_group = asyncio.run(adapter.parse(row, event(group_id=777)))

    assert accepted is not None and accepted.source.sender_id == "123"
    assert wrong_endpoint is None and wrong_group is None


def test_conflicting_keyword_is_not_returned_for_execution():
    service_id = uuid4()
    row = SimpleNamespace(service_id=service_id, event_url="ws://llbot:3001")
    repository, native, replies = MagicMock(), MagicMock(), MagicMock()
    repository.keyword_conflicts.return_value = True
    adapter = FreqtradeQqAdapter(repository, native, replies)
    adapter.records[service_id] = configured_record()

    result = asyncio.run(adapter.parse(row, event()))

    assert result is None
    native.execute.assert_not_called()


def test_buy_transport_timeout_enqueues_one_unknown_result_with_configured_interval():
    service_id = uuid4()
    row = SimpleNamespace(service_id=service_id, event_url="ws://llbot:3001")
    repository, native, replies = MagicMock(), MagicMock(), MagicMock()
    native.execute.side_effect = TimeoutError("native timeout")
    adapter = FreqtradeQqAdapter(repository, native, replies)
    request = command(interval=17)

    asyncio.run(adapter.execute(row, request))

    native.execute.assert_called_once_with(service_id, request)
    replies.enqueue.assert_called_once_with(
        service_id,
        request,
        "买卖请求结果未确认，请查询币成交，勿重复下单。",
        17,
    )
