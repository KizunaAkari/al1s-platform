from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from al1s.components.host.app import create_host
from al1s.components.status_capability import StatusCapability
from tests.test_component_host_drivers import settings


def test_host_mutation_identity_and_operation_scoped_readonly_status_are_separate(tmp_path):
    config = settings(tmp_path)
    app = create_host(config)
    identity = uuid4()
    token = StatusCapability("x" * 32).issue(identity, datetime.now(UTC) + timedelta(minutes=45))
    with TestClient(app) as api:
        assert api.get("/inventory").status_code == 401
        assert api.post("/operations", json={"operation_id": str(identity)},
                        headers={"Authorization": "Bearer " + token}).status_code == 401
        status = api.get(f"/status/{identity}", headers={"Authorization": "Bearer " + token,
                                                        "Origin": "https://platform.local"})
        assert status.status_code == 200 and status.json()["state"] == "unknown"
        assert "payload" not in status.json()
        assert api.get(f"/status/{uuid4()}", headers={"Authorization": "Bearer " + token}).status_code == 401
        assert api.get(f"/status/{identity}", headers={"Authorization": "Bearer " + token,
                                                      "Origin": "https://other.local"}).status_code == 403


def test_inventory_can_skip_bot_probe_without_changing_default_or_auth(tmp_path, monkeypatch):
    import al1s.components.host.app as host_app
    import al1s.components.host.bot_inventory as bot_module

    calls = []
    monkeypatch.setattr(host_app, "inventory", lambda _deployment: [{"component": "platform"}])
    monkeypatch.setattr(
        bot_module,
        "bot_inventory",
        lambda _deployment: calls.append(True) or [{"component": "qq-bot"}],
    )
    config = settings(tmp_path).model_copy(update={"bots": object()})
    with TestClient(create_host(config)) as api:
        assert api.get("/inventory?include_bots=false").status_code == 401
        headers = {"Authorization": "Bearer " + "x" * 32}
        assert api.get("/inventory?include_bots=false", headers=headers).json() == {
            "items": [{"component": "platform"}]
        }
        assert calls == []
        assert api.get("/inventory", headers=headers).json() == {
            "items": [{"component": "platform"}, {"component": "qq-bot"}]
        }
        assert len(calls) == 1
