from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from al1s.components.contracts import ComponentReleaseInput, ComponentTarget
from al1s.components.inventory import ComponentInventoryService
from al1s.components.inventory import terminal_entries
from al1s.components.types import NativeComponent, TerminalProfile


def release(**changes):
    return ComponentReleaseInput.model_validate(
        {
            "release_id": uuid4(),
            "component": "postgresql",
            "version": "17.12",
            "architecture": "amd64",
            "size_bytes": 123,
            "sha256": "a" * 64,
            "candidate_image": "postgres:17.12-alpine",
            "expected_image_id": "sha256:" + "b" * 64,
            "native_version": "17.12",
            "compatible_from": "17",
            "data_policy": "same-format",
            **changes,
        }
    )


def test_release_and_target_reject_unbound_or_arbitrary_values():
    assert release().component == "postgresql"
    for bad in [
        dict(candidate_image="postgres:latest"),
        dict(candidate_image="other:17"),
        dict(size_bytes=5 * 1024**3 + 1),
        dict(command="rm"),
        dict(architecture="unknown"),
    ]:
        with pytest.raises(ValidationError):
            release(**bad)
    with pytest.raises(ValidationError):
        ComponentTarget(kind="terminal", terminal_id="")
    with pytest.raises(ValidationError):
        ComponentTarget(kind="platform", terminal_id=uuid4())
    assert ComponentTarget(kind="platform").scope == "platform"


def test_inventory_is_batched_and_distinguishes_maa_agent_and_offline_observation():
    now = datetime(2026, 10, 4, tzinfo=UTC)
    identity = uuid4()

    class Profiles:
        calls = 0

        def list_linux_profiles(self, limit, cursor):
            self.calls += 1
            assert limit == 20 and cursor is None
            return [
                TerminalProfile(
                    identity,
                    "Linux",
                    "offline",
                    "agent-r36",
                    "arm64",
                    now,
                    {"maa": {"version": "v5.12.1", "diagnostic": "secret"}},
                )
            ], None

    class Host:
        calls = 0

        def inventory(self):
            self.calls += 1
            return [
                NativeComponent(
                    "postgresql",
                    "17.11",
                    "amd64",
                    "healthy",
                    now,
                    "postgres:17",
                    "sha256:" + "a" * 64,
                    "c" * 64,
                )
            ]

    profiles, host = Profiles(), Host()
    page = ComponentInventoryService(profiles, host).list()
    maa = next(row for row in page.items if row.component == "maa")
    agent = next(row for row in page.items if row.component == "linux-terminal")
    assert maa.version == "v5.12.1" and agent.version == "agent-r36"
    assert maa.status == "offline" and maa.observed_at == now
    assert maa.upgrade_unit == "linux-terminal" and maa.target.terminal_id == identity
    assert "secret" not in page.model_dump_json()
    assert profiles.calls == host.calls == 1


def test_host_failure_does_not_hide_terminal_inventory():
    class Profiles:
        def list_linux_profiles(self, limit, cursor):
            return [], None

    class Host:
        def inventory(self):
            raise OSError("private-token")

    page = ComponentInventoryService(Profiles(), Host()).list()
    assert len(page.items) == 4 and all(row.status == "unknown" for row in page.items)
    assert "private-token" not in page.model_dump_json()


def test_online_agent_does_not_imply_maa_execution_is_healthy():
    from al1s.components.inventory import terminal_entries

    rows = terminal_entries(
        TerminalProfile(
            uuid4(),
            "Linux",
            "online",
            "agent-r36",
            "arm64",
            datetime.now(UTC),
            {
                "maa": {
                    "version": "v5.12.1",
                    "available": True,
                    "execution_ready": False,
                    "diagnostic": "private-token",
                }
            },
        )
    )
    assert rows[0].status == "healthy" and rows[1].status == "unhealthy"
    assert "private-token" not in rows[1].model_dump_json()
def test_terminal_machine_architecture_maps_to_docker_release_architecture():
    from al1s.components.types import TerminalProfile
    from uuid import uuid4
    from datetime import UTC, datetime
    item=TerminalProfile(uuid4(),"Linux","online","agent","aarch64",datetime.now(UTC),{})
    assert all(row.architecture=="arm64" for row in terminal_entries(item))
