from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from al1s.api.freqtrade import router
from al1s.freqtrade.contracts import DEFAULT_COMMANDS, FreqtradeRecord, FreqtradeSettings
from al1s.meme_workshop.runtime import create_runtime_app

TOKEN = "runtime-control-test-token-at-least-32-bytes"


def record(*, enabled=False, row_version=0):
    settings = FreqtradeSettings(
        enabled=enabled,
        event_ws_url="ws://llbot:3001" if enabled else None,
        groups=["888"] if enabled else [],
    )
    return FreqtradeRecord(
        settings=settings, commands=dict(DEFAULT_COMMANDS), row_version=row_version
    )


def payload(*, enabled=True):
    return {
        "settings": FreqtradeSettings(
            enabled=enabled,
            event_ws_url="ws://llbot:3001" if enabled else None,
            groups=["888"] if enabled else [],
        ).model_dump(mode="json"),
        "commands": dict(DEFAULT_COMMANDS),
    }


def direct_app(repository, client, adapter, worker=None):
    app = FastAPI()
    app.include_router(router)
    app.state.freqtrade_repository = repository
    app.state.freqtrade_client = client
    app.state.freqtrade_adapter = adapter
    app.state.quant_worker = worker or MagicMock(refresh=AsyncMock())
    return TestClient(app)


def test_get_settings_and_status_use_repository_and_native_client():
    service_id = uuid4()
    repository, native, adapter = MagicMock(), MagicMock(), MagicMock()
    repository.get.return_value = record()
    native.applied.return_value = 0
    adapter.applied.return_value = 0
    native.status.return_value = {
        "state": "ready",
        "version": "2026.9",
        "active_accounts": 1,
        "total_accounts": 2,
        "max_active_accounts": 5,
        "rss_bytes": 1024,
        "cpu_percent": 0.5,
        "market": {"state": "available", "last_success": None, "error": None},
    }
    client = direct_app(repository, native, adapter)

    settings = client.get(f"/bots/freqtrade/settings/{service_id}")
    status = client.get("/bots/freqtrade/status")

    assert settings.status_code == 200
    assert settings.json()["desired"]["row_version"] == 0
    assert settings.json()["synchronized"] is False
    assert status.status_code == 200 and status.json()["version"] == "2026.9"
    repository.get.assert_called_once_with(service_id)
    native.status.assert_called_once_with()


def test_put_validates_if_match_passes_operation_and_refreshes_after_native_apply():
    service_id = uuid4()
    repository, native, adapter, worker = (
        MagicMock(), MagicMock(), MagicMock(), MagicMock()
    )

    def save(_service, settings, commands, expected_version, operation_id):
        return FreqtradeRecord(
            settings=settings, commands=commands, row_version=expected_version + 1
        )

    repository.save.side_effect = save
    native.activate.return_value = 1
    native.applied.return_value = 1
    adapter.applied.return_value = 0
    order = []

    def activate(*args):
        order.append("activate")
        return 1

    async def refresh():
        order.append("refresh")

    native.activate.side_effect = activate
    worker.refresh = AsyncMock(side_effect=refresh)
    client = direct_app(repository, native, adapter, worker)

    response = client.put(
        f"/bots/freqtrade/settings/{service_id}",
        json=payload(),
        headers={"If-Match": "0", "Idempotency-Key": "operation-1"},
    )

    assert response.status_code == 200
    repository.save.assert_called_once()
    saved = repository.save.call_args.kwargs
    assert saved["expected_version"] == 0 and saved["operation_id"] == "operation-1"
    assert order == ["activate", "refresh"]
    assert response.json()["applied_version"] == 1
    assert response.json()["synchronized"] is False


def test_put_rejects_invalid_if_match_and_does_not_save():
    repository, native, adapter = MagicMock(), MagicMock(), MagicMock()
    client = direct_app(repository, native, adapter)

    response = client.put(
        f"/bots/freqtrade/settings/{uuid4()}",
        json=payload(),
        headers={"If-Match": "not-a-version", "Idempotency-Key": "operation-1"},
    )

    assert response.status_code == 400
    repository.save.assert_not_called()


def test_put_conflict_returns_409_without_calling_native_client():
    service_id = uuid4()
    repository, native, adapter, worker = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    repository.save.side_effect = ValueError("freqtrade_settings_conflict")
    client = direct_app(repository, native, adapter, worker)

    response = client.put(
        f"/bots/freqtrade/settings/{service_id}",
        json=payload(),
        headers={"If-Match": "0", "Idempotency-Key": "operation-conflict"},
    )

    assert response.status_code == 409
    native.activate.assert_not_called()
    worker.refresh.assert_not_called()


def test_runtime_freqtrade_routes_require_the_private_control_header():
    service_id = uuid4()
    repository, native, adapter = MagicMock(), MagicMock(), MagicMock()
    repository.get.return_value = record()
    native.applied.return_value = 0
    adapter.applied.return_value = 0
    app = create_runtime_app(
        MagicMock(),
        MagicMock(),
        TOKEN,
        role="qq",
        freqtrade_repository=repository,
        freqtrade_client=native,
        freqtrade_adapter=adapter,
    )
    client = TestClient(app)
    path = f"/api/v1/bots/freqtrade/settings/{service_id}"

    assert client.get(path).status_code == 401
    authorized = client.get(path, headers={"X-AL1S-Meme-Control": TOKEN})
    assert authorized.status_code == 200 and authorized.json()["desired"]["row_version"] == 0
