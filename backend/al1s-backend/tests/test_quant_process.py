from uuid import uuid4
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from al1s.adapters.postgres.quant_settings import QuantSettingsRecord
from al1s.quant.commands import DEFAULT_COMMANDS
from al1s.quant.contracts import QuantSettings
from al1s.quant.process import create_process_app
from al1s.quant.process_client import QuantProcessClient
from al1s.quant.gateway import QuantGateway
from al1s.quant.health import QuantHealth


def test_process_requires_internal_token_and_acknowledges_exact_version():
    app = create_process_app("x" * 32)
    client = TestClient(app)
    service = uuid4()
    record = QuantSettingsRecord(
        settings=QuantSettings(), commands=DEFAULT_COMMANDS, row_version=1, mapping_version=1
    )
    path = f"/internal/config/{service}"
    assert client.get(path).status_code == 401
    assert client.put(
        path, json=record.model_dump(mode="json"), headers={"X-Quant-Control": "x" * 32}
    ).json() == {"row_version": 1, "mapping_version": 1}
    assert client.get(path, headers={"X-Quant-Control": "x" * 32}).json() == {
        "row_version": 1,
        "mapping_version": 1,
    }


def test_client_rejects_missing_or_invalid_ack():
    client = QuantProcessClient(
        "/tmp/quant.sock", "x" * 32,
        request=lambda method, path, body, headers: (200, b'{"row_version":"bad"}'),
    )
    try:
        client.applied_versions(uuid4())
    except ValueError as error:
        assert str(error) == "quant_process_response_invalid"
    else:
        raise AssertionError("invalid acknowledgement was accepted")


def test_process_accepts_only_an_authorized_snapshot_from_qq_gateway():
    handler = MagicMock(return_value="模拟账户已开户")
    app = create_process_app("x" * 32, command_handler=handler)
    client = TestClient(app)
    service = uuid4()
    settings = QuantSettings(enabled=True, event_ws_url="ws://llbot:3001", groups=["123456"])
    record = QuantSettingsRecord(settings=settings, commands=DEFAULT_COMMANDS,
                                 row_version=1, mapping_version=1)
    client.put(f"/internal/config/{service}", json=record.model_dump(mode="json"),
               headers={"X-Quant-Control": "x" * 32})
    payload = {"source": {"message_id": "1", "scope_kind": "qq_group", "target_id": "123456",
                          "sender_id": "222222", "guild_id": None},
               "action": "open", "arguments": [], "mapping_version": 1, "settings_version": 1,
               "event_timestamp": 1790000000, "normalized_keyword": "模拟开户",
               "settings": settings.model_dump(mode="json"), "commands": DEFAULT_COMMANDS}
    path = f"/internal/command/{service}"
    response = client.post(path, json=payload, headers={"X-Quant-Control": "x" * 32})
    assert response.status_code == 200 and response.json()["content"] == "模拟账户已开户"
    assert handler.call_args.args[1].source.sender_id == "222222"
    payload["source"]["target_id"] = "987654"
    assert client.post(path, json=payload, headers={"X-Quant-Control": "x" * 32}).status_code == 422
    assert handler.call_count == 1


def test_process_client_sends_frozen_qq_identity_and_configuration():
    import json

    service = uuid4()
    settings = QuantSettings(enabled=True, event_ws_url="ws://llbot:3001", groups=["123456"])
    gateway = QuantGateway()
    gateway.activate(service, QuantSettingsRecord(settings=settings, commands=DEFAULT_COMMANDS,
                                                   row_version=1, mapping_version=1))
    event = {"post_type": "message", "message_type": "group", "self_id": 999,
             "user_id": 222222, "group_id": 123456, "message_id": 1, "time": 1790000000,
             "message": [{"type": "text", "data": {"text": "模拟开户"}}]}
    captured = []
    def request(method, path, body, headers):
        captured.append((method, path, json.loads(body), headers))
        return 200, b'{"content":"ok"}'
    client = QuantProcessClient("/tmp/quant.sock", "x" * 32, request=request)
    assert client.execute(service, gateway.dispatch(service, event, set())) == "ok"
    assert captured[0][2]["source"]["sender_id"] == "222222"
    assert captured[0][2]["settings_version"] == 1


def test_process_recovers_interrupted_orders_before_polling():
    executor = MagicMock()
    app = create_process_app("x" * 32, executor=executor)
    with TestClient(app):
        assert executor.orders.recover_interrupted.call_count == 1


def test_health_endpoint_requires_token_and_reports_actual_observations():
    health = QuantHealth(database=lambda: True, source=lambda: {}, engine=lambda: {"version": "0.3.71", "entitlement_supported": True})
    client = TestClient(create_process_app("x" * 32, health=health))
    assert client.get("/internal/health").status_code == 401
    result = client.get("/internal/health", headers={"X-Quant-Control": "x" * 32})
    assert result.status_code == 200 and result.json()["database"] == "available"


def test_known_domain_errors_are_explained_without_leaking_internal_details():
    handler = MagicMock(side_effect=ValueError("quant_market_session_closed"))
    app = create_process_app("x" * 32, command_handler=handler)
    client = TestClient(app)
    service = uuid4()
    settings = QuantSettings(enabled=True, event_ws_url="ws://llbot:3001", groups=["123456"])
    record = QuantSettingsRecord(settings=settings, commands=DEFAULT_COMMANDS, row_version=1, mapping_version=1)
    client.put(f"/internal/config/{service}", json=record.model_dump(mode="json"), headers={"X-Quant-Control": "x" * 32})
    payload = {"source": {"message_id": "1", "scope_kind": "qq_group", "target_id": "123456", "sender_id": "222222", "guild_id": None},
               "action": "buy", "arguments": ["600000", "100"], "mapping_version": 1, "settings_version": 1,
               "event_timestamp": 1790000000, "normalized_keyword": "模拟买入", "settings": settings.model_dump(mode="json"), "commands": DEFAULT_COMMANDS}
    response = client.post(f"/internal/command/{service}", json=payload, headers={"X-Quant-Control": "x" * 32})
    assert response.status_code == 200 and "交易时段" in response.json()["content"]
