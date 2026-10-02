"""Bind an Android installation to its target within registration's transaction."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from al1s.execution.errors import ConflictError, NotFoundError
from al1s.execution.ports import ExecutionUnitOfWork
from al1s.execution.resource_events import record_resource_event
from al1s.execution.security import digest_external_identifier
from al1s.execution.types import (
    RegistrationGrant,
    TargetDeviceMode,
    TargetDeviceRecord,
    TargetIdentifierSource,
    TerminalRecord,
)


def enroll_android_target(
    uow: ExecutionUnitOfWork,
    *,
    grant: RegistrationGrant,
    terminal: TerminalRecord,
    installation_id: UUID,
    display_name: str,
    now: datetime,
    correlation_id: UUID,
) -> TargetDeviceRecord:
    identifier_digest, display_hint = digest_external_identifier(
        TargetIdentifierSource.APK_INSTALLATION.value,
        str(installation_id),
    )
    requested_target = (
        uow.target_devices.get_active(grant.target_device_id, for_update=True)
        if grant.target_device_id is not None
        else None
    )
    if grant.target_device_id is not None and requested_target is None:
        raise NotFoundError("target_device")

    identifier = uow.target_identifiers.find_active(
        TargetIdentifierSource.APK_INSTALLATION,
        identifier_digest,
        for_update=True,
    )
    if identifier is not None and identifier.source_terminal_id != terminal.terminal_id:
        raise ConflictError(
            "android_installation_terminal_conflict",
            "Android installation is already owned by another terminal",
        )
    if (
        identifier is not None
        and identifier.target_device_id is not None
        and requested_target is not None
        and identifier.target_device_id != requested_target.device_id
    ):
        raise ConflictError(
            "android_installation_target_conflict",
            "Android installation is already bound to another target device",
        )

    if requested_target is not None:
        target = requested_target
    elif identifier is not None and identifier.target_device_id is not None:
        existing_target = uow.target_devices.get_active(
            identifier.target_device_id,
            for_update=True,
        )
        if existing_target is None:
            raise ConflictError(
                "android_installation_target_missing",
                "Android installation is bound to a deleted target device",
            )
        target = existing_target
    else:
        target = uow.target_devices.add(uuid4(), display_name, now)

    target_installation = uow.target_identifiers.find_bound_to_target(
        TargetIdentifierSource.APK_INSTALLATION,
        target.device_id,
        for_update=True,
    )
    if target_installation is not None and (
        identifier is None or target_installation.identifier_id != identifier.identifier_id
    ):
        raise ConflictError(
            "android_target_installation_conflict",
            "Target device is already bound to another Android installation",
        )

    if identifier is None:
        identifier = uow.target_identifiers.add(
            uuid4(),
            terminal.terminal_id,
            TargetIdentifierSource.APK_INSTALLATION,
            identifier_digest,
            display_hint,
        )
    if identifier.target_device_id is None:
        bound = uow.target_identifiers.bind(
            identifier.identifier_id,
            identifier.row_version,
            target.device_id,
            now,
        )
        if bound is None:
            raise ConflictError(
                "stale_device_identifier",
                "Android installation binding changed concurrently",
            )
        identifier = bound

    if target.mode is TargetDeviceMode.UNASSIGNED:
        uow.leases.release_expired(
            terminal_id=None,
            target_device_id=target.device_id,
            now=now,
        )
        if uow.leases.has_active(target_device_id=target.device_id, now=now):
            raise ConflictError(
                "target_device_in_use",
                "Target device has an active lease",
            )
        activated = uow.target_devices.update_mode(
            target.device_id,
            target.row_version,
            TargetDeviceMode.STANDALONE,
            terminal.terminal_id,
            now,
        )
        if activated is None:
            raise ConflictError(
                "stale_target_device",
                "Target device changed concurrently",
            )
        target = activated
    elif (
        target.mode is TargetDeviceMode.STANDALONE
        and target.managing_terminal_id != terminal.terminal_id
    ):
        raise ConflictError(
            "android_target_manager_conflict",
            "Target device is managed by another Android terminal",
        )

    record_resource_event(
        uow,
        event_type="android_terminal.target_bound.v1",
        aggregate_type="target_device",
        aggregate_id=target.device_id,
        correlation_id=correlation_id,
        occurred_at=now,
        payload={
            "device_id": str(target.device_id),
            "terminal_id": str(terminal.terminal_id),
            "identifier_id": str(identifier.identifier_id),
            "mode": target.mode.value,
        },
        action="android_terminal.target.bind",
        actor_type="terminal_registration",
        actor_id=terminal.terminal_id,
    )
    return target

