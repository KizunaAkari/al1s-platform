"""Immutable snapshots for QQ event dispatch and configuration acknowledgements."""

from dataclasses import dataclass, replace
from threading import RLock
from types import MappingProxyType
from uuid import UUID

from al1s.adapters.postgres.quant_settings import QuantSettingsRecord
from al1s.quant.commands import QuantCommand, parse_quant_event, validate_mappings
from al1s.quant.contracts import QuantSettings


@dataclass(frozen=True, slots=True)
class GatewaySnapshot:
    enabled: bool
    private_enabled: bool
    event_ws_url: str | None
    groups: frozenset[str]
    commands: MappingProxyType
    row_version: int
    mapping_version: int
    settings: QuantSettings


class QuantGateway:
    def __init__(self) -> None:
        self._lock = RLock()
        self._snapshots: dict[UUID, GatewaySnapshot] = {}

    def applied_versions(self, service_id: UUID) -> tuple[int, int]:
        with self._lock:
            current = self._snapshots.get(service_id)
            return (current.row_version, current.mapping_version) if current else (0, 0)

    def discard(self, service_id: UUID) -> None:
        with self._lock:
            self._snapshots.pop(service_id, None)

    def matches_connection(self, service_id: UUID, event_url: str) -> bool:
        with self._lock:
            current = self._snapshots.get(service_id)
            return bool(current and current.enabled and current.event_ws_url == event_url)

    def activate(self, service_id: UUID, desired: QuantSettingsRecord) -> tuple[int, int]:
        if desired.row_version < 1 or desired.mapping_version < 1:
            raise ValueError("quant_snapshot_invalid")
        validate_mappings(desired.commands, set())
        snapshot = GatewaySnapshot(
            desired.settings.enabled,
            desired.settings.private_enabled,
            desired.settings.event_ws_url,
            frozenset(desired.settings.groups),
            MappingProxyType(dict(desired.commands)),
            desired.row_version,
            desired.mapping_version,
            desired.settings.model_copy(deep=True),
        )
        with self._lock:
            current = self._snapshots.get(service_id)
            if current and current.row_version > snapshot.row_version:
                return current.row_version, current.mapping_version
            if current and current.row_version == snapshot.row_version and current != snapshot:
                raise ValueError("quant_snapshot_version_conflict")
            self._snapshots[service_id] = snapshot
            return snapshot.row_version, snapshot.mapping_version

    def dispatch(
        self, service_id: UUID, event: object, meme_words: set[str]
    ) -> QuantCommand | None:
        with self._lock:
            snapshot = self._snapshots.get(service_id)
        if snapshot is None or not snapshot.enabled:
            return None
        command = parse_quant_event(event, snapshot.commands, meme_words, snapshot.mapping_version)
        if command is None:
            return None
        command = replace(
            command,
            settings_version=snapshot.row_version,
            settings=snapshot.settings.model_copy(deep=True),
            command_words=tuple(snapshot.commands.items()),
        )
        if command.source.scope_kind == "qq_private":
            return command if snapshot.private_enabled else None
        return command if command.source.target_id in snapshot.groups else None
