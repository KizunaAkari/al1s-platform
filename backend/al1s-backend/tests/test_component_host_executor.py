from datetime import UTC, datetime
from uuid import uuid4

import pytest

from al1s.components.host.journal import HostJournal
from al1s.components.host.executor import HostComponentExecutor


class Driver:
    def __init__(self):
        self.calls = []

    def prepare(self, identity, payload):
        self.calls.append("prepare")
        return {"backup_id": str(identity), "original_image": "old"}

    def switch(self, identity, payload, point):
        self.calls.append("switch")

    def verify(self, identity, payload, point):
        self.calls.append("verify")
        return True

    def recover(self, identity, payload, point):
        self.calls.append("recover")
        return "failed_safe"


def test_host_journal_survives_platform_down_and_never_replays_switch(tmp_path):
    identity = uuid4()
    driver = Driver()
    journal = HostJournal(tmp_path)
    executor = HostComponentExecutor(journal, driver)
    result = executor.execute(identity, {"operation": {"component": "postgresql"}})
    assert result["state"] == "succeeded" and result["proof"] is True
    assert driver.calls == ["prepare", "switch", "verify"]
    restored = HostComponentExecutor(HostJournal(tmp_path), driver)
    assert (
        restored.execute(identity, {"operation": {"component": "postgresql"}})["state"]
        == "succeeded"
    )
    assert driver.calls == ["prepare", "switch", "verify"]


def test_interrupted_switch_is_unknown_until_recovery_verifies_evidence(tmp_path):
    identity = uuid4()
    journal = HostJournal(tmp_path)
    journal.save(
        identity,
        {
            "operation_id": str(identity),
            "state": "switching",
            "stage": "switching",
            "payload": {},
            "recovery": {"backup_id": str(identity)},
            "proof": False,
        },
    )
    driver = Driver()
    executor = HostComponentExecutor(journal, driver)
    assert executor.status(identity)["state"] == "unknown"
    assert driver.calls == []
    assert executor.reconcile(identity)["state"] == "failed_safe"
    assert driver.calls == ["recover"]


def test_safe_error_does_not_expose_command_or_private_paths(tmp_path):
    class Failed(Driver):
        def prepare(self, *args):
            raise RuntimeError("private-token at C:/secret")

    result = HostComponentExecutor(HostJournal(tmp_path), Failed()).execute(uuid4(), {})
    assert result["state"] == "failed_safe"
    assert "private-token" not in str(result) and "C:/secret" not in str(result)


def test_reserved_payload_cannot_change_before_execution(tmp_path):
    executor = HostComponentExecutor(HostJournal(tmp_path), Driver())
    identity = uuid4()
    executor.reserve(identity, {"release": "first"})
    with pytest.raises(ValueError, match="component_operation_identity_conflict"):
        executor.execute(identity, {"release": "other"})
    assert executor.driver.calls == []
