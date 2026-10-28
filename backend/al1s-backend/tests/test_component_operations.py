from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from al1s.components.contracts import ComponentInventoryEntry, ComponentTarget
from al1s.components.operations import compatible_release, receipt_transition
from al1s.components.status_capability import StatusCapability
from al1s.execution.errors import AuthenticationError, ConflictError
from tests.test_component_inventory import release


def current(**values):
    return ComponentInventoryEntry(
        component="postgresql",
        upgrade_unit="postgresql",
        target=ComponentTarget(kind="platform"),
        display_name="PostgreSQL",
        version="17.11",
        status="healthy",
        architecture="amd64",
        source_identity="c" * 64,
        **values,
    )


def test_compatibility_rejects_wrong_architecture_major_and_unhealthy_source():
    assert compatible_release(current(), release()) == []
    assert "architecture_mismatch" in compatible_release(current(), release(architecture="arm64"))
    assert "postgres_major_migration_required" in compatible_release(
        current(), release(native_version="18.0")
    )
    assert "source_version_incompatible" in compatible_release(
        current(), release(compatible_from="16")
    )


def test_receipts_cannot_blindly_release_unknown_or_regress_completed_state():
    assert receipt_transition("switching", "unknown", proof=False) == "unknown"
    assert receipt_transition("unknown", "succeeded", proof=True) == "succeeded"
    with pytest.raises(ConflictError):
        receipt_transition("unknown", "failed_safe", proof=False)
    with pytest.raises(ConflictError):
        receipt_transition("succeeded", "preparing", proof=True)


def test_status_capability_is_operation_bound_short_lived_and_read_only():
    now = datetime(2026, 10, 4, tzinfo=UTC)
    signer = StatusCapability("x" * 32, now=lambda: now)
    identity = uuid4()
    token = signer.issue(identity, now + timedelta(minutes=45))
    assert signer.verify(token, identity) == identity
    for invalid, target in [(token + "x", identity), (token, uuid4())]:
        with pytest.raises(AuthenticationError):
            signer.verify(invalid, target)
    signer.now = lambda: now + timedelta(hours=1)
    with pytest.raises(AuthenticationError):
        signer.verify(token, identity)
