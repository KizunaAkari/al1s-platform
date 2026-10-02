from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.execution.android_target_enrollment import enroll_android_target
from al1s.execution.types import TargetDeviceMode


def test_new_installation_binds_standalone_target_in_callers_transaction() -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    terminal_id, target_id, identifier_id = uuid4(), uuid4(), uuid4()
    uow = MagicMock()
    uow.target_identifiers.find_active.return_value = None
    uow.target_identifiers.find_bound_to_target.return_value = None
    uow.target_devices.add.return_value = SimpleNamespace(
        device_id=target_id, mode=TargetDeviceMode.UNASSIGNED, row_version=1
    )
    uow.target_identifiers.add.return_value = SimpleNamespace(
        identifier_id=identifier_id, target_device_id=None, row_version=1
    )
    uow.target_identifiers.bind.return_value = SimpleNamespace(identifier_id=identifier_id)
    uow.leases.has_active.return_value = False
    active = SimpleNamespace(
        device_id=target_id, mode=TargetDeviceMode.STANDALONE,
        managing_terminal_id=terminal_id,
    )
    uow.target_devices.update_mode.return_value = active

    result = enroll_android_target(
        uow,
        grant=SimpleNamespace(target_device_id=None),
        terminal=SimpleNamespace(terminal_id=terminal_id),
        installation_id=uuid4(),
        display_name="phone",
        now=now,
        correlation_id=uuid4(),
    )

    assert result is active
    uow.target_identifiers.bind.assert_called_once()
    uow.target_devices.update_mode.assert_called_once()
    uow.outbox.add.assert_called_once()
    uow.audit.add.assert_called_once()
    uow.commit.assert_not_called()
