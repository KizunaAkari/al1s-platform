import hashlib
import re
from uuid import UUID

from al1s.components.contracts import ComponentInventoryEntry, ComponentTarget, InventoryPage
from al1s.components.ports import ComponentHost, InventoryReader
from al1s.components.types import NativeComponent, TerminalProfile

PLATFORM_COMPONENTS = ("platform", "postgresql", "s3", "mqtt")


def version(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", value)
        else None
    )


def native_entry(item: NativeComponent) -> ComponentInventoryEntry:
    return ComponentInventoryEntry(
        component=item.component,
        upgrade_unit=item.component,
        target=ComponentTarget(kind="platform"),
        display_name=item.component,
        version=version(item.version),
        architecture=item.architecture,
        status=item.status if item.status in {"healthy", "unhealthy", "unknown"} else "unknown",
        observed_at=item.observed_at,
        image=item.image,
        image_id=item.image_id,
        source_identity=item.source_identity,
        storage_layout=item.storage_layout,
    )


def terminal_entries(item: TerminalProfile) -> list[ComponentInventoryEntry]:
    target = ComponentTarget(kind="terminal", terminal_id=item.terminal_id)
    status = "healthy" if item.status == "online" else "offline"
    maa = item.details.get("maa")
    maa_version = version(maa.get("version")) if isinstance(maa, dict) else None
    source = hashlib.sha256(
        f"{item.terminal_id}|{item.agent_version}|{maa_version}".encode()
    ).hexdigest()
    shared = {
        "target": target,
        "display_name": item.display_name,
        "status": status,
        "architecture": {"aarch64": "arm64", "x86_64": "amd64"}.get(
            item.architecture or "", item.architecture
        ),
        "observed_at": item.observed_at,
        "source_identity": source,
        "upgrade_unit": "linux-terminal",
    }
    rows = [
        ComponentInventoryEntry(
            component="linux-terminal", version=version(item.agent_version), **shared
        ),
        ComponentInventoryEntry(component="maa", version=maa_version, **shared),
    ]
    if status == "healthy":
        readiness = "unknown"
        if isinstance(maa, dict):
            if maa.get("available") is True and maa.get("execution_ready") is True:
                readiness = "healthy"
            elif maa.get("available") is False or maa.get("execution_ready") is False:
                readiness = "unhealthy"
        rows[1] = rows[1].model_copy(update={"status": readiness})
    return rows


class ComponentInventoryService:
    def __init__(self, profiles: InventoryReader, host: ComponentHost):
        self.profiles, self.host = profiles, host

    def get(self, target: ComponentTarget, component: str) -> ComponentInventoryEntry | None:
        if target.kind == "terminal" and target.terminal_id is not None:
            profile = self.profiles.get_linux_profile(target.terminal_id)
            return (
                next((row for row in terminal_entries(profile) if row.component == component), None)
                if profile
                else None
            )
        try:
            native = next(
                (item for item in self.host.inventory() if item.component == component), None
            )
            return native_entry(native) if native else None
        except (OSError, ValueError, RuntimeError):
            return None

    def list(self, limit: int = 20, cursor: UUID | None = None) -> InventoryPage:
        limit = max(1, min(20, limit))
        try:
            observed = {
                item.component: item
                for item in self.host.inventory()
                if item.component in PLATFORM_COMPONENTS
            }
        except (OSError, ValueError, RuntimeError):
            observed = {}
        rows = [
            native_entry(observed.get(key, NativeComponent(key, None, None, "unknown", None)))
            for key in PLATFORM_COMPONENTS
        ]
        profiles, next_cursor = self.profiles.list_linux_profiles(limit, cursor)
        for item in profiles:
            rows.extend(terminal_entries(item))
        return InventoryPage(items=rows, next_cursor=next_cursor)
