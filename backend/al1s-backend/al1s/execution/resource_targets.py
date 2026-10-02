from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from al1s.execution.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from al1s.execution.resource_base import ResourceServiceBase
from al1s.execution.resource_events import record_resource_event
from al1s.execution.security import (
    digest_external_identifier,
    parse_terminal_credential,
)
from al1s.execution.types import (
    TargetDeviceMode,
    TargetDeviceRecord,
    TargetIdentifierRecord,
    TargetIdentifierSource,
    TerminalAcceptanceStatus,
    TerminalServiceStatus,
    TerminalType,
)


class TargetDeviceOperations(ResourceServiceBase):
    def create_target_device(
        self, *, display_name: str, correlation_id: UUID
    ) -> TargetDeviceRecord:
        now = self._now()
        device_id = uuid4()
        with self._uow_factory() as uow:
            target = uow.target_devices.add(device_id, display_name, now)
            record_resource_event(
                uow,
                event_type="target_device.created.v1",
                aggregate_type="target_device",
                aggregate_id=device_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={"device_id": str(device_id), "platform": "android"},
                action="target_device.create",
                actor_type="operator",
                actor_id=None,
            )
            uow.commit()
        return target

    def rename_target_device(
        self,
        *,
        device_id: UUID,
        expected_version: int,
        display_name: str,
        correlation_id: UUID,
    ) -> TargetDeviceRecord:
        name = display_name.strip()
        if not 1 <= len(name) <= 120:
            raise InvalidRequestError(
                "invalid_target_device_name", "Phone name must be 1..120 characters"
            )
        now = self._now()
        with self._uow_factory() as uow:
            target = uow.target_devices.get_active(device_id, for_update=True)
            if target is None:
                raise NotFoundError("target_device")
            if target.row_version != expected_version:
                raise ConflictError("stale_target_device", "Phone changed; refresh before renaming")
            if target.display_name == name:
                return target
            updated = uow.target_devices.rename(device_id, expected_version, name, now)
            if updated is None:
                raise ConflictError("stale_target_device", "Phone changed; refresh before renaming")
            record_resource_event(
                uow,
                event_type="target_device.display_name_changed.v1",
                aggregate_type="target_device",
                aggregate_id=device_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={"device_id": str(device_id), "row_version": updated.row_version},
                action="target_device.display_name.update",
                actor_type="operator",
                actor_id=None,
            )
            uow.commit()
        return updated

    def list_adb_discoveries(
        self, *, terminal_id: UUID, after_id: UUID | None, limit: int
    ) -> list[TargetIdentifierRecord]:
        with self._uow_factory() as uow:
            if uow.terminals.get_active(terminal_id) is None:
                raise NotFoundError("terminal")
            return uow.target_identifiers.list_adb(
                terminal_id=terminal_id, after_id=after_id, limit=limit
            )

    def connect_adb_discovery(
        self,
        *,
        identifier_id: UUID,
        expected_identifier_version: int,
        display_name: str,
        correlation_id: UUID,
    ) -> TargetDeviceRecord:
        """Explicit first association, never merge or switch an existing device owner."""
        now = self._now()
        with self._uow_factory() as uow:
            observed = uow.target_identifiers.get_active(identifier_id)
            if observed is None:
                raise NotFoundError("target_device_identifier")
            terminal = uow.terminals.get_active(observed.source_terminal_id, for_update=True)
            if terminal is None:
                raise NotFoundError("terminal")
            identifier = uow.target_identifiers.get_active(identifier_id, for_update=True)
            if identifier is None:
                raise NotFoundError("target_device_identifier")
            if (
                terminal.terminal_type is not TerminalType.LINUX
                or identifier.source_type is not TargetIdentifierSource.ADB_SERIAL
            ):
                raise ConflictError(
                    "adb_linux_required", "Only Linux ADB discoveries can be connected"
                )
            if identifier.target_device_id is not None:
                target = uow.target_devices.get_active(identifier.target_device_id)
                if (
                    target is not None
                    and target.mode is TargetDeviceMode.MOUNTED
                    and target.managing_terminal_id == terminal.terminal_id
                ):
                    return target
                raise ConflictError(
                    "target_already_associated",
                    "Existing association requires its own handover workflow",
                )
            if identifier.row_version != expected_identifier_version:
                raise ConflictError(
                    "stale_device_identifier", "Device discovery changed; refresh before connecting"
                )
            if (
                terminal.service_status is not TerminalServiceStatus.ONLINE
                or terminal.acceptance_status is not TerminalAcceptanceStatus.ACCEPTING
            ):
                raise ConflictError(
                    "terminal_unavailable", "Terminal must be online and accepting work"
                )
            target = uow.target_devices.add(uuid4(), display_name, now)
            bound = uow.target_identifiers.bind(
                identifier_id, expected_identifier_version, target.device_id, now
            )
            if bound is None:
                raise ConflictError(
                    "stale_device_identifier", "Device discovery changed concurrently"
                )
            mounted = uow.target_devices.update_mode(
                target.device_id,
                target.row_version,
                TargetDeviceMode.MOUNTED,
                terminal.terminal_id,
                now,
            )
            if mounted is None:
                raise ConflictError(
                    "stale_target_device", "Device association changed concurrently"
                )
            record_resource_event(
                uow,
                event_type="target_device.adb_connected.v1",
                aggregate_type="target_device",
                aggregate_id=target.device_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "device_id": str(target.device_id),
                    "identifier_id": str(identifier_id),
                    "terminal_id": str(terminal.terminal_id),
                },
                action="target_device.adb.connect",
                actor_type="operator",
                actor_id=None,
            )
            uow.commit()
            return mounted

    def discover_target_identifier(
        self,
        *,
        credential: str,
        source_type: TargetIdentifierSource,
        raw_identifier: str,
        correlation_id: UUID,
        adb_state: str | None = None,
    ) -> TargetIdentifierRecord:
        parsed = parse_terminal_credential(credential)
        identifier_digest, display_hint = digest_external_identifier(
            source_type.value, raw_identifier
        )
        now = self._now()
        if adb_state is not None and (
            source_type is not TargetIdentifierSource.ADB_SERIAL
            or adb_state not in {"device", "offline", "unauthorized", "other"}
        ):
            raise InvalidRequestError("invalid_adb_state", "ADB state is invalid")
        try:
            with self._uow_factory() as uow:
                terminal = self._authenticate(uow, parsed.identifier, parsed.secret)
                existing = uow.target_identifiers.find_active(source_type, identifier_digest)
                if existing is not None:
                    if existing.source_terminal_id != terminal.terminal_id:
                        raise ConflictError(
                            "device_identifier_source_conflict",
                            "Device identifier was already reported by another terminal",
                        )
                    if adb_state is not None:
                        observed = uow.target_identifiers.observe(
                            existing.identifier_id, adb_state, now
                        )
                        uow.commit()
                        return observed
                    return existing
                identifier = uow.target_identifiers.add(
                    uuid4(),
                    terminal.terminal_id,
                    source_type,
                    identifier_digest,
                    display_hint,
                )
                if adb_state is not None:
                    identifier = uow.target_identifiers.observe(
                        identifier.identifier_id, adb_state, now
                    )
                record_resource_event(
                    uow,
                    event_type="target_device.identifier_discovered.v1",
                    aggregate_type="target_device_identifier",
                    aggregate_id=identifier.identifier_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                    payload={
                        "identifier_id": str(identifier.identifier_id),
                        "source_terminal_id": str(terminal.terminal_id),
                        "source_type": source_type.value,
                    },
                    action="target_device.identifier.discover",
                    actor_type="terminal",
                    actor_id=terminal.terminal_id,
                )
                uow.commit()
        except IntegrityError as exc:
            raise ConflictError(
                "device_identifier_conflict", "Device identifier changed concurrently"
            ) from exc
        return identifier

    def report_adb_observations(
        self, *, credential: str, items: tuple[tuple[UUID, str], ...]
    ) -> int:
        if not 1 <= len(items) <= 100 or len({item[0] for item in items}) != len(items):
            raise InvalidRequestError("invalid_adb_observation_batch", "Batch is invalid")
        if any(state not in {"device", "offline", "unauthorized", "other"} for _, state in items):
            raise InvalidRequestError("invalid_adb_state", "ADB state is invalid")
        parsed = parse_terminal_credential(credential)
        with self._uow_factory() as uow:
            terminal = self._authenticate(uow, parsed.identifier, parsed.secret)
            try:
                updated = uow.target_identifiers.observe_batch(
                    terminal.terminal_id, items, self._now()
                )
            except ValueError as exc:
                raise ConflictError(
                    "adb_observation_identifier_not_owned", "Identifier is not active for terminal"
                ) from exc
            uow.commit()
            return updated

    def bind_target_identifier(
        self,
        *,
        device_id: UUID,
        identifier_id: UUID,
        expected_identifier_version: int,
        correlation_id: UUID,
    ) -> TargetIdentifierRecord:
        now = self._now()
        with self._uow_factory() as uow:
            target = uow.target_devices.get_active(device_id, for_update=True)
            if target is None:
                raise NotFoundError("target_device")
            identifier = uow.target_identifiers.get_active(identifier_id, for_update=True)
            if identifier is None:
                raise NotFoundError("target_device_identifier")
            if identifier.target_device_id is not None:
                if identifier.target_device_id == device_id:
                    return identifier
                raise ConflictError(
                    "device_identifier_already_bound",
                    "Device identifier is already bound to another target device",
                )
            bound = uow.target_identifiers.bind(
                identifier_id, expected_identifier_version, device_id, now
            )
            if bound is None:
                raise ConflictError(
                    "stale_device_identifier", "Device identifier changed concurrently"
                )
            record_resource_event(
                uow,
                event_type="target_device.identifier_bound.v1",
                aggregate_type="target_device",
                aggregate_id=device_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "device_id": str(device_id),
                    "identifier_id": str(identifier_id),
                    "source_type": bound.source_type.value,
                },
                action="target_device.identifier.bind",
                actor_type="operator",
                actor_id=None,
            )
            uow.commit()
        return bound

    def set_target_mode(
        self,
        *,
        device_id: UUID,
        expected_version: int,
        mode: TargetDeviceMode,
        managing_terminal_id: UUID | None,
        correlation_id: UUID,
    ) -> TargetDeviceRecord:
        now = self._now()
        with self._uow_factory() as uow:
            target = uow.target_devices.get_active(device_id, for_update=True)
            if target is None:
                raise NotFoundError("target_device")
            if target.row_version != expected_version:
                raise ConflictError("stale_target_device", "Target device version is stale")
            if (
                target.mode in {TargetDeviceMode.STANDALONE, TargetDeviceMode.MOUNTED}
                and mode in {TargetDeviceMode.STANDALONE, TargetDeviceMode.MOUNTED}
                and (target.mode is not mode or target.managing_terminal_id != managing_terminal_id)
            ):
                raise ConflictError(
                    "target_mode_direct_switch_forbidden",
                    "Target device must be unassigned before changing execution subject",
                )
            if target.mode is mode and target.managing_terminal_id == managing_terminal_id:
                return target
            if uow.target_devices.has_pending_control(device_id, target.managing_terminal_id):
                raise ConflictError(
                    "target_control_pending",
                    "Finish outstanding tasks, offline permits and editor sessions before handoff",
                )
            manager_ids = self._lock_and_validate_target_managers(
                uow,
                target=target,
                mode=mode,
                managing_terminal_id=managing_terminal_id,
            )
            uow.leases.release_expired(terminal_id=None, target_device_id=device_id, now=now)
            if uow.leases.has_active(target_device_id=device_id, now=now):
                raise ConflictError("target_device_in_use", "Target device has an active lease")
            for manager_id in manager_ids:
                uow.leases.release_expired(terminal_id=manager_id, target_device_id=None, now=now)
                if uow.leases.has_active(terminal_id=manager_id, now=now):
                    raise ConflictError(
                        "target_manager_in_use", "Managing terminal has an active lease"
                    )
            updated = uow.target_devices.update_mode(
                device_id, expected_version, mode, managing_terminal_id, now
            )
            if updated is None:
                raise ConflictError("stale_target_device", "Target device changed concurrently")
            record_resource_event(
                uow,
                event_type="target_device.mode_changed.v1",
                aggregate_type="target_device",
                aggregate_id=device_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "device_id": str(device_id),
                    "mode": mode.value,
                    "managing_terminal_id": (
                        str(managing_terminal_id) if managing_terminal_id else None
                    ),
                    "row_version": updated.row_version,
                },
                action="target_device.mode.change",
                actor_type="operator",
                actor_id=None,
            )
            uow.commit()
        return updated

    def list_target_devices(
        self, *, after_id: UUID | None = None, limit: int = 50
    ) -> list[TargetDeviceRecord]:
        with self._uow_factory() as uow:
            devices = uow.target_devices.list_active(after_id=after_id, limit=limit)
            rows = uow.target_devices.availability_rows(tuple(d.device_id for d in devices))
        now = self._now()
        return [
            self._with_availability(device, rows.get(device.device_id), now) for device in devices
        ]

    def get_target_device(self, device_id: UUID) -> TargetDeviceRecord:
        with self._uow_factory() as uow:
            device = uow.target_devices.get_active(device_id)
            if device is None:
                raise NotFoundError("target_device")
            observation = uow.target_devices.availability_rows((device_id,)).get(device_id)
        return self._with_availability(device, observation, self._now())

    @staticmethod
    def _with_availability(
        device: TargetDeviceRecord,
        observation: tuple[str | None, str | None, datetime | None] | None,
        now: datetime,
    ) -> TargetDeviceRecord:
        if device.mode is not TargetDeviceMode.MOUNTED:
            return replace(device, availability_reason="not_mounted")
        terminal_status, adb_state, observed_at = observation or (None, None, None)
        if terminal_status != TerminalServiceStatus.ONLINE.value:
            return replace(device, availability="unknown", availability_reason="terminal_offline")
        if observed_at is None:
            return replace(device, availability="unknown", availability_reason="not_observed")
        if observed_at < now - timedelta(seconds=30):
            return replace(
                device,
                availability="disconnected",
                availability_reason="adb_observation_stale",
                availability_observed_at=observed_at,
            )
        if adb_state == "device":
            return replace(
                device,
                availability="connected",
                availability_reason=None,
                availability_observed_at=observed_at,
            )
        if adb_state == "unauthorized":
            status, reason = "unauthorized", "adb_unauthorized"
        elif adb_state == "offline":
            status, reason = "disconnected", "adb_offline"
        else:
            status, reason = "unknown", "adb_state_unknown"
        return replace(
            device,
            availability=status,
            availability_reason=reason,
            availability_observed_at=observed_at,
        )

    def require_quick_test_assignment(self, *, terminal_id: UUID, target_device_id: UUID) -> None:
        """Reject a quick test unless the selected terminal currently manages the target."""
        with self._uow_factory() as uow:
            terminal = uow.terminals.get_active(terminal_id)
            if terminal is None:
                raise NotFoundError("terminal")
            if terminal.service_status is not TerminalServiceStatus.ONLINE:
                raise ConflictError("terminal_offline", "Terminal is offline")
            if terminal.acceptance_status is not TerminalAcceptanceStatus.ACCEPTING:
                raise ConflictError(
                    "terminal_not_accepting", "Terminal is not accepting quick tests"
                )
            target = uow.target_devices.get_active(target_device_id)
            if target is None:
                raise NotFoundError("target_device")
            if target.managing_terminal_id != terminal_id:
                raise ConflictError(
                    "target_terminal_mismatch",
                    "Target device is not managed by the selected terminal",
                )
