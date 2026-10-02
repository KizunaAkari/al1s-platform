from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from al1s.execution.errors import ExecutionDomainError
from al1s.execution.host_management import (
    CancelMaintenanceRequest,
    HostManagerConnection,
    RestartRequest,
)
from al1s.execution.maintenance_service import MaintenanceService
from al1s.execution.maintenance_types import MaintenanceRecord


def test_cancel_requires_current_version_and_host_confirmation():
    now, terminal, identity = datetime.now(UTC), uuid4(), uuid4()
    row = MaintenanceRecord(
        identity,
        terminal,
        "restart_host",
        "a" * 64,
        "accepted",
        now,
        now + timedelta(seconds=600),
        None,
        None,
        None,
        1,
        None,
    )
    store = MagicMock()
    store.get.return_value = store.observe.return_value = row
    service = MaintenanceService(
        store,
        {
            terminal: HostManagerConnection(url="https://host", token="x" * 32),
        },
        now=lambda: now,
    )
    with patch("al1s.execution.maintenance_service.HostManagerClient") as factory:
        with pytest.raises(ExecutionDomainError):
            service.cancel(terminal, CancelMaintenanceRequest(command_id=identity, version=2))
        factory.return_value.request.assert_not_called()
        factory.return_value.request.side_effect = ExecutionDomainError("offline", "offline", 503)
        with pytest.raises(ExecutionDomainError):
            service.cancel(terminal, CancelMaintenanceRequest(command_id=identity, version=1))
        # Only local observations happened; no made-up cancelled remote result was saved.
        assert all(len(call.args) == 3 for call in store.observe.call_args_list)


def test_offline_preflight_never_reserves():
    terminal = uuid4()
    now = datetime.now(UTC)
    store = MagicMock()
    store.get.return_value = None
    service = MaintenanceService(
        store,
        {
            terminal: HostManagerConnection(url="https://host", token="x" * 32),
        },
        now=lambda: now,
    )
    body = RestartRequest(
        command_id=uuid4(),
        action="restart_host",
        expires_at=(now + timedelta(seconds=590)).timestamp(),
        confirm_interrupt=True,
        expected_boot_id="boot",
        expected_container_id="c",
        expected_container_started_at="start",
    )
    with patch("al1s.execution.maintenance_service.HostManagerClient") as factory:
        factory.return_value.request.side_effect = ExecutionDomainError("offline", "offline", 503)
        with pytest.raises(ExecutionDomainError):
            service.submit(terminal, body)
        store.reserve.assert_not_called()
        factory.return_value.request.assert_called_once_with("/v1/health")


def test_upgrade_risk_without_confirmation_never_reserves_or_posts():
    terminal = uuid4()
    now = datetime.now(UTC)
    store = MagicMock()
    store.get.return_value = None
    service = MaintenanceService(
        store,
        {
            terminal: HostManagerConnection(url="https://host", token="x" * 32),
        },
        now=lambda: now,
    )
    body = RestartRequest(
        command_id=uuid4(),
        action="restart_host",
        expires_at=(now + timedelta(seconds=590)).timestamp(),
        confirm_interrupt=True,
        expected_boot_id="boot",
        expected_container_id="c",
        expected_container_started_at="start",
    )
    with patch("al1s.execution.maintenance_service.HostManagerClient") as factory:
        factory.return_value.request.return_value = {
            "boot_id": "boot",
            "container_id": "c",
            "container_started_at": "start",
            "unresolved_upgrade_id": "release-1",
        }
        with pytest.raises(ExecutionDomainError, match="Confirm the current unresolved"):
            service.submit(terminal, body)
        store.reserve.assert_not_called()
        factory.return_value.request.assert_called_once_with("/v1/health")


def test_unknown_post_is_never_automatically_repeated():
    terminal = uuid4()
    now = datetime.now(UTC)
    store = MagicMock()
    store.get.return_value = None
    store.reserve.side_effect = lambda value: (value, True)
    service = MaintenanceService(
        store,
        {
            terminal: HostManagerConnection(url="https://host", token="x" * 32),
        },
        now=lambda: now,
    )
    body = RestartRequest(
        command_id=uuid4(),
        action="restart_host",
        expires_at=(now + timedelta(seconds=590)).timestamp(),
        confirm_interrupt=True,
        expected_boot_id="boot",
        expected_container_id="c",
        expected_container_started_at="start",
    )
    with patch("al1s.execution.maintenance_service.HostManagerClient") as factory:
        factory.return_value.request.side_effect = [
            {"boot_id": "boot", "container_id": "c", "container_started_at": "start"},
            ExecutionDomainError("unknown", "unknown", 503),
        ]
        service.submit(terminal, body)
        value = store.reserve.call_args.args[0]
        store.get.return_value = value
        service.submit(terminal, body)
        assert factory.return_value.request.call_count == 2
        store.due.return_value = [value]
        factory.return_value.request.side_effect = ExecutionDomainError("missing", "missing", 404)
        assert service.poll_once() == 1
        assert factory.return_value.request.call_args.args == (f"/v1/commands/{body.command_id}",)
