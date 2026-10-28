"""Pure qualification and release rules shared by the path service and tests."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID


@dataclass(frozen=True)
class DevicePath:
    terminal_id: UUID
    kind: str
    instance_id: UUID
    observed_at: datetime
    heartbeat_at: datetime
    ready_count: int
    control_ready: bool
    authority_protocol: int
    capabilities: frozenset[str]


def choose_path(
    paths: list[DevicePath], required: set[str], now: datetime,
) -> DevicePath | None:
    qualified = [p for p in paths if (
        p.kind in {"linux", "android"} and p.authority_protocol == 1
        and p.control_ready and p.ready_count >= 2
        and now - timedelta(seconds=30) <= p.observed_at <= now
        and now - timedelta(seconds=90) <= p.heartbeat_at <= now
        and required <= p.capabilities
    )]
    return min(qualified, key=lambda p: (p.kind != "linux", str(p.terminal_id)), default=None)


def handoff_allowed(*, physical_release: bool, unsettled_work: bool, editor_open: bool) -> bool:
    # A timer or an expired platform lease cannot establish physical release.
    return physical_release and not unsettled_work and not editor_open
