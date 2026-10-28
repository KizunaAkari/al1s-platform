from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from al1s.api.quant import router
from al1s.meme_workshop.runtime import create_runtime_app
from al1s.quant.commands import DEFAULT_COMMANDS
from al1s.quant.contracts import QuantSettings
from al1s.quant.gateway import QuantGateway
from tests.test_quant_settings import configured as configured


def test_mapping_save_confirms_gateway_and_reports_desired_and_applied(configured):
    repository, service = configured
    app = FastAPI()
    app.include_router(router)
    app.state.quant_settings = repository
    app.state.quant_gateway = QuantGateway()
    app.state.quant_process = QuantGateway()
    client = TestClient(app)
    url = f"/bots/quant/settings/{service}"
    payload = {"settings": QuantSettings().model_dump(mode="json"), "commands": DEFAULT_COMMANDS}
    saved = client.put(url, json=payload, headers={"If-Match": "0", "Idempotency-Key": "k1"})
    assert saved.status_code == 200
    assert saved.json()["desired"]["row_version"] == 1
    assert saved.json()["applied"] == {"row_version": 1, "mapping_version": 1}
    assert saved.json()["process_applied"] == {"row_version": 1, "mapping_version": 1}
    assert client.get(url).json()["synchronized"] is True


def test_failed_gateway_ack_never_reports_success(configured):
    repository, service = configured
    app = FastAPI()
    app.include_router(router)
    app.state.quant_settings = repository

    class BrokenGateway(QuantGateway):
        def activate(self, service_id, desired):
            raise RuntimeError("gateway unavailable")

    app.state.quant_gateway = BrokenGateway()
    app.state.quant_process = QuantGateway()
    client = TestClient(app)
    url = f"/bots/quant/settings/{service}"
    payload = {"settings": QuantSettings().model_dump(mode="json"), "commands": DEFAULT_COMMANDS}
    assert (
        client.put(
            url, json=payload, headers={"If-Match": "0", "Idempotency-Key": "k1"}
        ).status_code
        == 503
    )
    view = client.get(url).json()
    assert view["desired"]["row_version"] == 1 and view["synchronized"] is False


def test_quant_process_failure_does_not_report_success(configured):
    repository, service = configured
    app = FastAPI()
    app.include_router(router)
    app.state.quant_settings = repository
    app.state.quant_gateway = QuantGateway()
    app.state.quant_process = None
    client = TestClient(app)
    url = f"/bots/quant/settings/{service}"
    payload = {"settings": QuantSettings().model_dump(mode="json"), "commands": DEFAULT_COMMANDS}
    assert (
        client.put(
            url, json=payload, headers={"If-Match": "0", "Idempotency-Key": "k1"}
        ).status_code
        == 503
    )
    assert client.get(url).json()["synchronized"] is False


def test_enabled_settings_are_not_reported_applied_without_qq_connection(configured):
    repository, service = configured
    app = FastAPI()
    app.include_router(router)
    app.state.quant_settings = repository
    app.state.quant_gateway = QuantGateway()
    app.state.quant_process = QuantGateway()
    app.state.quant_worker = None
    client = TestClient(app)
    payload = {"settings": QuantSettings(enabled=True, event_ws_url="ws://llbot:3001").model_dump(mode="json"),
               "commands": DEFAULT_COMMANDS}
    url = f"/bots/quant/settings/{service}"
    assert client.put(url, json=payload, headers={"If-Match": "0", "Idempotency-Key": "k1"}).status_code == 503
    assert client.get(url).json()["synchronized"] is False


def test_qq_runtime_quant_api_is_protected_by_existing_control_token(configured):
    repository, service = configured
    token = "runtime-control-test-token-at-least-32-bytes"
    app = create_runtime_app(
        MagicMock(),
        MagicMock(),
        token,
        role="qq",
        quant_settings=repository,
        quant_gateway=QuantGateway(),
        quant_process=QuantGateway(),
    )
    client = TestClient(app)
    path = f"/api/v1/bots/quant/settings/{service}"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-AL1S-Meme-Control": token}).status_code == 200


def test_credential_api_never_returns_secret_and_rejects_unknown_provider():
    app = FastAPI()
    app.include_router(router)
    store = MagicMock()
    store.configured.return_value = {"tushare"}
    app.state.quant_credentials = store
    client = TestClient(app)
    response = client.put("/bots/quant/credentials/tushare", json={"secret": "private-test-key"})
    assert response.status_code == 200 and "private-test-key" not in response.text
    store.save.assert_called_once_with("tushare", "private-test-key")
    assert client.get("/bots/quant/credentials").json() == {"tushare": True, "gugudata": False}
    assert client.put("/bots/quant/credentials/other", json={"secret": "private-test-key"}).status_code == 422
