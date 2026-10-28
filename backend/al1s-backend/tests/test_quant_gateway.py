from uuid import uuid4

from al1s.adapters.postgres.quant_settings import QuantSettingsRecord
from al1s.quant.commands import DEFAULT_COMMANDS
from al1s.quant.contracts import QuantSettings
from al1s.quant.gateway import QuantGateway


def event(text):
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": 999,
        "user_id": 12345,
        "group_id": 123456,
        "message_id": 1,
        "time": 1790000000,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def test_snapshot_activation_ack_and_inflight_action_stability():
    gateway, service = QuantGateway(), uuid4()
    assert gateway.applied_versions(service) == (0, 0)
    settings = QuantSettings(enabled=True, groups=["123456"], event_ws_url="ws://llbot:3001")
    first = QuantSettingsRecord(
        settings=settings, commands=DEFAULT_COMMANDS, row_version=1, mapping_version=1
    )
    assert gateway.activate(service, first) == (1, 1)
    assert gateway.matches_connection(service, "ws://llbot:3001")
    assert not gateway.matches_connection(service, "ws://other:3001")
    in_flight = gateway.dispatch(service, event("模拟买入 600000 100"), set())
    second = QuantSettingsRecord(
        settings=settings,
        commands={**DEFAULT_COMMANDS, "buy": "买入"},
        row_version=2,
        mapping_version=2,
    )
    assert gateway.activate(service, second) == (2, 2)
    assert gateway.applied_versions(service) == (2, 2)
    assert gateway.dispatch(service, event("模拟买入 600000 100"), set()) is None
    assert gateway.dispatch(service, event("买入 600000 100"), set()).action == "buy"
    assert in_flight.action == "buy" and in_flight.mapping_version == 1
    assert in_flight.settings_version == 1
    assert in_flight.settings.groups == ["123456"]
    assert dict(in_flight.command_words)["buy"] == "模拟买入"
    assert gateway.activate(service, first) == (2, 2)


def test_disabled_unauthorized_and_conflicting_commands_are_not_dispatched():
    gateway, service = QuantGateway(), uuid4()
    settings = QuantSettings(enabled=True, event_ws_url="ws://llbot:3001", groups=["123456"])
    gateway.activate(
        service,
        QuantSettingsRecord(
            settings=settings, commands=DEFAULT_COMMANDS, row_version=1, mapping_version=1
        ),
    )
    assert gateway.dispatch(service, event("模拟开户"), {"模拟开户"}) is None
    outside = event("模拟开户")
    outside["group_id"] = 777
    assert gateway.dispatch(service, outside, set()) is None
    gateway.activate(
        service,
        QuantSettingsRecord(
            settings=QuantSettings(enabled=False),
            commands=DEFAULT_COMMANDS,
            row_version=2,
            mapping_version=1,
        ),
    )
    assert gateway.dispatch(service, event("模拟开户"), set()) is None
    gateway.discard(service)
    assert gateway.applied_versions(service) == (0, 0)
