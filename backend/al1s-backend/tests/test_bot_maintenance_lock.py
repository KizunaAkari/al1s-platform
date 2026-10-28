from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.components.host.bot_maintenance import BotMaintenanceGuard
from al1s.components.host.executor import HostComponentExecutor
from al1s.components.host.journal import HostJournal


def test_unknown_qq_blocks_only_qq_commands(tmp_path):
    journal = HostJournal(tmp_path)
    identity = uuid4()
    journal.save(
        identity,
        {
            "operation_id": str(identity),
            "state": "unknown",
            "payload": {"operation": {"target": {"kind": "bot", "bot_alias": "qq"}}},
        },
    )
    guard = BotMaintenanceGuard(tmp_path)
    with pytest.raises(ValueError, match="unresolved"):
        guard.assert_command_allowed("qq")
    with guard.exclusive("discord"):
        guard.assert_command_allowed("discord")


def test_file_locks_are_independent_and_same_scope_is_exclusive(tmp_path):
    journal = HostJournal(tmp_path)
    with journal.exclusive("bot:qq"):
        with journal.exclusive("bot:discord"):
            pass
        with pytest.raises(OSError), journal.exclusive("bot:qq"):
            pass


def test_new_operation_cannot_replace_unresolved_scope_owner(tmp_path):
    executor = HostComponentExecutor(HostJournal(tmp_path), MagicMock())
    payload = {"operation": {"target": {"kind": "bot", "bot_alias": "qq"}}}
    first = uuid4()
    executor.reserve(first, payload)
    with pytest.raises(ValueError, match="unresolved"):
        executor.reserve(uuid4(), payload)
    assert executor.reserve(first, payload)["operation_id"] == str(first)
    executor.reserve(uuid4(), {"operation": {"target": {"kind": "bot", "bot_alias": "discord"}}})


def test_platform_lock_also_excludes_both_bot_control_mutations(tmp_path):
    guard = BotMaintenanceGuard(tmp_path)
    with HostJournal(tmp_path).exclusive("platform"):
        with pytest.raises(OSError), guard.exclusive("qq"):
            pass
        with pytest.raises(OSError), guard.exclusive("discord"):
            pass


def test_interrupted_journal_write_keeps_scope_locked_and_never_replays(tmp_path, monkeypatch):
    import os

    identity = uuid4()
    replace = os.replace

    def interrupted(source, destination):
        if destination == tmp_path / (str(identity) + ".json"):
            raise OSError("interrupted")
        replace(source, destination)

    monkeypatch.setattr(os, "replace", interrupted)
    executor = HostComponentExecutor(HostJournal(tmp_path), MagicMock())
    payload = {"operation": {"target": {"kind": "bot", "bot_alias": "qq"}}}
    with pytest.raises(OSError):
        executor.reserve(identity, payload)
    with pytest.raises(ValueError, match="unresolved"):
        BotMaintenanceGuard(tmp_path).assert_command_allowed("qq")
    monkeypatch.setattr(os, "replace", replace)
    assert executor.reserve(identity, payload)["state"] == "unknown"
