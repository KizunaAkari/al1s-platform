from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from al1s.execution.errors import ConflictError, InvalidRequestError
from al1s.execution.services import ExecutionResourceService
from al1s.execution.types import (
    TerminalAcceptanceStatus,
    TerminalRecord,
    TerminalServiceStatus,
    TerminalType,
)


def terminal(*, name_is_custom: bool = False, name_version: int = 0) -> TerminalRecord:
    return TerminalRecord(
        terminal_id=uuid4(), installation_id=uuid4(), terminal_type=TerminalType.LINUX,
        display_name="Device report", service_status=TerminalServiceStatus.OFFLINE,
        acceptance_status=TerminalAcceptanceStatus.ACCEPTING, agent_version="test",
        current_capability_profile_id=None, row_version=5,
        created_at=datetime.now(UTC), last_seen_at=None, deleted_at=None,
        name_is_custom=name_is_custom, name_version=name_version,
    )


def service_fixture(record: TerminalRecord):
    uow = SimpleNamespace(terminals=Mock(), outbox=Mock(), audit=Mock(), commit=Mock())
    uow.terminals.get_active.return_value = record
    uow.terminals.rename.return_value = replace(
        record, display_name="Desk phone", name_is_custom=True,
        name_version=record.name_version + 1, row_version=record.row_version + 1,
    )
    return ExecutionResourceService(lambda: nullcontext(uow)), uow


def test_admin_name_is_trimmed_and_audited_without_changing_identity():
    original = terminal()
    service, uow = service_fixture(original)
    updated = service.rename_terminal(
        terminal_id=original.terminal_id, expected_name_version=0,
        display_name="  Desk phone  ", correlation_id=uuid4(),
    )
    assert updated.terminal_id == original.terminal_id
    assert updated.display_name == "Desk phone" and updated.name_is_custom
    uow.terminals.get_active.assert_called_once_with(original.terminal_id, for_update=True)
    uow.terminals.rename.assert_called_once_with(original.terminal_id, 0, "Desk phone")
    uow.outbox.add.assert_called_once()
    uow.audit.add.assert_called_once()
    uow.commit.assert_called_once()


@pytest.mark.parametrize("name", ["   ", "x" * 121])
def test_invalid_admin_name_does_not_open_transaction(name: str):
    service, uow = service_fixture(terminal())
    with pytest.raises(InvalidRequestError):
        service.rename_terminal(
            terminal_id=uuid4(), expected_name_version=0,
            display_name=name, correlation_id=uuid4(),
        )
    uow.terminals.get_active.assert_not_called()


def test_stale_name_version_rejects_concurrent_admin_change():
    current = terminal(name_is_custom=True, name_version=2)
    service, uow = service_fixture(current)
    with pytest.raises(ConflictError):
        service.rename_terminal(
            terminal_id=current.terminal_id, expected_name_version=1,
            display_name="New name", correlation_id=uuid4(),
        )
    uow.terminals.rename.assert_not_called()
    uow.commit.assert_not_called()


@pytest.mark.parametrize("custom, expected", [(False, "Device update"), (True, "Device report")])
def test_reregistration_keeps_admin_name(custom: bool, expected: str):
    existing = terminal(name_is_custom=custom)
    uow = SimpleNamespace(terminals=Mock())
    uow.terminals.find_active_by_installation_id_for_update.return_value = existing
    uow.terminals.refresh_registration.return_value = existing
    ExecutionResourceService._resolve_registration_terminal(
        uow, installation_id=existing.installation_id,
        terminal_type=existing.terminal_type, display_name="Device update",
        agent_version="next", now=datetime.now(UTC),
    )
    assert uow.terminals.refresh_registration.call_args.args[2] == expected
