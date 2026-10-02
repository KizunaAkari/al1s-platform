from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.bots.runtime_status import BotRuntimeService
from al1s.bots.types import BotServiceKind

NOW = datetime(2026, 10, 1, 14, tzinfo=UTC)


def setup(kind=BotServiceKind.ONEBOT_GATEWAY):
    service_id, version_id = uuid4(), uuid4()
    services, connections, post = MagicMock(), MagicMock(), MagicMock()
    services.get_service.return_value = SimpleNamespace(
        service_id=service_id, kind=kind, enabled=True, applied_config_version_id=version_id
    )
    connections.read.return_value = SimpleNamespace(
        settings={"ONEBOT_BASE_URL": "http://al1s-llbot:3000"}, secret="secret"
    )
    post.return_value = {"status": "ok", "retcode": 0, "data": {"online": True, "good": True}}
    services.list_health_reports.return_value = []
    runtime = BotRuntimeService(services, connections, now=lambda: NOW, post=post)
    return runtime, services, connections, post, service_id, version_id


@pytest.mark.parametrize(
    "online,good,state,reason",
    [
        (True, True, "online", "online"),
        (False, True, "offline", "qq_not_logged_in"),
        (True, False, "offline", "qq_gateway_unhealthy"),
    ],
)
def test_qq_reads_live_boolean_state_with_short_budget(online, good, state, reason):
    runtime, _, _, post, identity, _ = setup()
    post.return_value["data"] = {"online": online, "good": good}
    status = runtime.read(identity)
    assert status.state == state and status.reason_code == reason
    assert status.observed_at == NOW
    post.assert_called_once_with(
        "http://al1s-llbot:3000/get_status", "secret", {}, timeout_seconds=3
    )
    assert "secret" not in str(status)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"status": "ok", "retcode": True},
        {"status": "ok", "retcode": 0, "data": {"online": "true", "good": True}},
    ],
)
def test_invalid_qq_state_cannot_be_online(payload):
    runtime, _, _, post, identity, _ = setup()
    post.return_value = payload
    assert runtime.read(identity).state == "unknown"


def test_network_error_is_bounded_and_does_not_expose_response_or_secret():
    runtime, _, _, post, identity, _ = setup()
    post.side_effect = TimeoutError("https://x/?token=secret")
    status = runtime.read(identity)
    assert status.state == "unknown" and status.reason_code == "qq_status_unavailable"
    assert "secret" not in str(status)


@pytest.mark.parametrize(
    "enabled,configured,state", [(False, True, "disabled"), (True, False, "unconfigured")]
)
def test_no_network_for_disabled_or_unconfigured(enabled, configured, state):
    runtime, services, reader, post, identity, _ = setup()
    services.get_service.return_value.enabled = enabled
    if not configured:
        services.get_service.return_value.applied_config_version_id = None
    assert runtime.read(identity).state == state
    post.assert_not_called()
    reader.read.assert_not_called()


@pytest.mark.parametrize(
    "age,ready,state,reason",
    [
        (20, True, "online", "online"),
        (91, True, "offline", "discord_heartbeat_stale"),
        (20, False, "offline", "discord_disconnected"),
    ],
)
def test_discord_requires_fresh_gateway_fact(age, ready, state, reason):
    runtime, services, _, post, identity, version = setup(BotServiceKind.DISCORD_BRIDGE)
    services.list_health_reports.return_value = [
        SimpleNamespace(
            config_version_id=version,
            reported_at=NOW - timedelta(seconds=age),
            diagnostics={"discord_ready": ready, "shutting_down": False},
            status="healthy",
        )
    ]
    result = runtime.read(identity)
    assert result.state == state and result.reason_code == reason
    assert services.list_health_reports.call_args.kwargs["limit"] == 1
    post.assert_not_called()


def test_missing_heartbeat_and_foreign_config_never_report_online():
    runtime, services, _, _, identity, _ = setup(BotServiceKind.DISCORD_BRIDGE)
    assert runtime.read(identity).reason_code == "discord_heartbeat_missing"
    services.list_health_reports.return_value = [
        SimpleNamespace(
            config_version_id=uuid4(), reported_at=NOW, diagnostics={"discord_ready": True}
        )
    ]
    assert runtime.read(identity).reason_code == "discord_config_mismatch"


def test_connection_error_only_projects_allowlisted_code():
    runtime, services, _, _, identity, version = setup(BotServiceKind.DISCORD_BRIDGE)
    report = SimpleNamespace(
        config_version_id=version,
        reported_at=NOW,
        diagnostics={
            "discord_ready": False,
            "runtime_error_code": "discord_login_timeout",
            "raw_error": "token=secret",
        },
    )
    services.list_health_reports.return_value = [report]
    assert runtime.read(identity).error_code == "discord_login_timeout"
    report.diagnostics["runtime_error_code"] = "token=secret"
    result = runtime.read(identity)
    assert result.error_code is None and "secret" not in str(result)
