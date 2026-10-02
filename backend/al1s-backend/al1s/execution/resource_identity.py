from __future__ import annotations

from datetime import timedelta
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
    parse_registration_code,
    parse_terminal_credential,
)
from al1s.execution.storage import StorageObservation
from al1s.execution.types import (
    ActionAvailability,
    CapabilityManifest,
    CapabilityProfileRecord,
    IssuedRegistrationGrant,
    RegisteredTerminal,
    TerminalAcceptanceStatus,
    TerminalManagementFacts,
    TerminalManagementRecord,
    TerminalRecord,
    TerminalServiceStatus,
    TerminalType,
)


class TerminalIdentityOperations(ResourceServiceBase):
    def create_registration_grant(
        self,
        *,
        allowed_terminal_type: TerminalType | None,
        target_device_id: UUID | None = None,
        ttl: timedelta,
        correlation_id: UUID,
    ) -> IssuedRegistrationGrant:
        if not timedelta(minutes=1) <= ttl <= timedelta(hours=24):
            raise InvalidRequestError(
                "invalid_registration_grant_ttl",
                "Registration grant TTL must be between 1 minute and 24 hours",
            )
        if target_device_id is not None and allowed_terminal_type is not TerminalType.ANDROID:
            raise InvalidRequestError(
                "android_target_grant_requires_android",
                "A target-bound registration grant must only allow Android terminals",
            )
        now = self._now()
        grant_id = uuid4()
        registration_code, digest = self._digester.issue(grant_id)
        expires_at = now + ttl
        with self._uow_factory() as uow:
            if target_device_id is not None:
                target = uow.target_devices.get_active(target_device_id, for_update=True)
                if target is None:
                    raise NotFoundError("target_device")
            uow.grants.add(
                grant_id,
                digest,
                allowed_terminal_type,
                target_device_id,
                expires_at,
            )
            record_resource_event(
                uow,
                event_type="terminal.registration_grant_created.v1",
                aggregate_type="terminal_registration_grant",
                aggregate_id=grant_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "grant_id": str(grant_id),
                    "allowed_terminal_type": (
                        allowed_terminal_type.value if allowed_terminal_type else None
                    ),
                    "target_device_id": (
                        str(target_device_id) if target_device_id is not None else None
                    ),
                    "expires_at": expires_at.isoformat(),
                },
                action="terminal.registration_grant.create",
                actor_type="operator",
                actor_id=None,
            )
            uow.commit()
        return IssuedRegistrationGrant(
            grant_id,
            registration_code,
            expires_at,
            target_device_id,
        )

    def register_terminal(
        self,
        *,
        registration_code: str,
        installation_id: UUID,
        terminal_type: TerminalType,
        display_name: str,
        agent_version: str,
        correlation_id: UUID,
    ) -> RegisteredTerminal:
        parsed = parse_registration_code(registration_code)
        now = self._now()
        try:
            with self._uow_factory() as uow:
                grant = uow.grants.get_for_update(parsed.identifier)
                self._validate_registration_grant(
                    grant,
                    parsed.secret,
                    terminal_type,
                    now,
                )
                assert grant is not None
                terminal = self._resolve_registration_terminal(
                    uow,
                    installation_id=installation_id,
                    terminal_type=terminal_type,
                    display_name=display_name,
                    agent_version=agent_version,
                    now=now,
                )
                target_device = (
                    self._enroll_android_target(
                        uow,
                        grant=grant,
                        terminal=terminal,
                        installation_id=installation_id,
                        display_name=display_name,
                        now=now,
                        correlation_id=correlation_id,
                    )
                    if terminal_type is TerminalType.ANDROID
                    else None
                )
                credential = self._replace_terminal_credential(uow, terminal.terminal_id, now)
                if not uow.grants.consume(
                    grant.grant_id, grant.row_version, terminal.terminal_id, now
                ):
                    raise ConflictError(
                        "registration_code_consumed", "Registration code has already been used"
                    )
                record_resource_event(
                    uow,
                    event_type="terminal.registered.v1",
                    aggregate_type="terminal",
                    aggregate_id=terminal.terminal_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                    payload={
                        "terminal_id": str(terminal.terminal_id),
                        "terminal_type": terminal.terminal_type.value,
                        "target_device_id": (
                            str(target_device.device_id) if target_device is not None else None
                        ),
                        "row_version": terminal.row_version,
                    },
                    action="terminal.register",
                    actor_type="terminal_registration",
                    actor_id=terminal.terminal_id,
                )
                uow.commit()
        except IntegrityError as exc:
            raise ConflictError(
                "terminal_registration_conflict", "Terminal registration conflicted"
            ) from exc
        return RegisteredTerminal(
            terminal=terminal,
            credential=credential,
            target_device=target_device,
        )

    def authenticate_terminal(self, credential: str) -> TerminalRecord:
        parsed = parse_terminal_credential(credential)
        with self._uow_factory() as uow:
            terminal = self._authenticate(uow, parsed.identifier, parsed.secret)
        return terminal

    def heartbeat(
        self,
        *,
        credential: str,
        expected_version: int,
        service_status: TerminalServiceStatus,
        acceptance_status: TerminalAcceptanceStatus,
        agent_version: str,
        correlation_id: UUID,
        storage: StorageObservation | None = None,
    ) -> TerminalRecord:
        parsed = parse_terminal_credential(credential)
        now = self._now()
        with self._uow_factory() as uow:
            terminal = self._authenticate(
                uow,
                parsed.identifier,
                parsed.secret,
                for_update=True,
            )
            if expected_version > terminal.row_version:
                raise ConflictError("stale_terminal", "Terminal version is stale")
            updated = uow.terminals.heartbeat(
                terminal.terminal_id,
                terminal.row_version,
                service_status,
                acceptance_status,
                agent_version,
                now,
                storage=storage,
                storage_alert_active=(
                    storage.alert_state(terminal.storage_alert_active)
                    if storage is not None
                    else terminal.storage_alert_active
                ),
            )
            if updated is None:
                raise ConflictError("stale_terminal", "Terminal changed concurrently")
            if (
                storage is not None
                and updated.storage_alert_active
                and not terminal.storage_alert_active
            ):
                record_resource_event(
                    uow,
                    event_type="terminal.storage_low.v1",
                    aggregate_type="terminal",
                    aggregate_id=updated.terminal_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                    action="terminal.storage_low",
                    actor_type="terminal",
                    actor_id=updated.terminal_id,
                    payload={
                        "total_bytes": storage.total_bytes,
                        "available_bytes": storage.available_bytes,
                    },
                )
            record_resource_event(
                uow,
                event_type="terminal.status_changed.v1",
                aggregate_type="terminal",
                aggregate_id=updated.terminal_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "terminal_id": str(updated.terminal_id),
                    "service_status": updated.service_status.value,
                    "acceptance_status": updated.acceptance_status.value,
                    "row_version": updated.row_version,
                },
                action="terminal.heartbeat",
                actor_type="terminal",
                actor_id=updated.terminal_id,
            )
            uow.commit()
        return updated

    def publish_capability(
        self,
        *,
        credential: str,
        revision: int,
        manifest: CapabilityManifest,
        correlation_id: UUID,
    ) -> CapabilityProfileRecord:
        if revision < 1:
            raise InvalidRequestError("invalid_capability_revision", "Revision must be positive")
        parsed = parse_terminal_credential(credential)
        now = self._now()
        manifest_hash = self._manifest_hash(manifest)
        with self._uow_factory() as uow:
            terminal = self._authenticate(uow, parsed.identifier, parsed.secret, for_update=True)
            existing = uow.capabilities.find_revision(terminal.terminal_id, revision)
            if existing is not None:
                if existing.manifest_hash != manifest_hash:
                    raise ConflictError(
                        "capability_revision_conflict",
                        "Capability revision already exists with different content",
                    )
                return existing
            if terminal.current_capability_profile_id is not None:
                current = uow.capabilities.get(terminal.current_capability_profile_id)
                if current is not None and revision <= current.revision:
                    raise ConflictError(
                        "stale_capability_revision",
                        "Capability revision must increase monotonically",
                    )
            profile = uow.capabilities.add(
                uuid4(), terminal.terminal_id, revision, manifest_hash, manifest
            )
            updated = uow.terminals.set_current_capability(
                terminal.terminal_id,
                terminal.row_version,
                profile.profile_id,
                manifest.agent_version,
            )
            if updated is None:
                raise ConflictError("stale_terminal", "Terminal changed concurrently")
            record_resource_event(
                uow,
                event_type="terminal.capabilities_changed.v1",
                aggregate_type="terminal",
                aggregate_id=terminal.terminal_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "terminal_id": str(terminal.terminal_id),
                    "profile_id": str(profile.profile_id),
                    "revision": profile.revision,
                    "manifest_hash": profile.manifest_hash,
                },
                action="terminal.capability.publish",
                actor_type="terminal",
                actor_id=terminal.terminal_id,
            )
            uow.commit()
        return profile

    def rotate_credential(
        self,
        *,
        credential: str,
        expected_version: int,
        correlation_id: UUID,
    ) -> tuple[TerminalRecord, str]:
        parsed = parse_terminal_credential(credential)
        now = self._now()
        with self._uow_factory() as uow:
            terminal = self._authenticate(uow, parsed.identifier, parsed.secret, for_update=True)
            if terminal.row_version != expected_version:
                raise ConflictError("stale_terminal", "Terminal version is stale")
            rotated = uow.terminals.heartbeat(
                terminal.terminal_id,
                terminal.row_version,
                terminal.service_status,
                terminal.acceptance_status,
                terminal.agent_version,
                now,
            )
            if rotated is None:
                raise ConflictError("stale_terminal", "Terminal changed concurrently")
            new_credential, digest = self._digester.issue(terminal.terminal_id)
            uow.credentials.revoke_active(terminal.terminal_id, now)
            uow.credentials.add(uuid4(), terminal.terminal_id, digest)
            record_resource_event(
                uow,
                event_type="terminal.credential_rotated.v1",
                aggregate_type="terminal",
                aggregate_id=terminal.terminal_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "terminal_id": str(terminal.terminal_id),
                    "row_version": rotated.row_version,
                },
                action="terminal.credential.rotate",
                actor_type="terminal",
                actor_id=terminal.terminal_id,
            )
            uow.commit()
        return rotated, new_credential

    def list_terminals(
        self, *, after_id: UUID | None = None, limit: int = 50
    ) -> list[TerminalRecord]:
        with self._uow_factory() as uow:
            return uow.terminals.list_active(after_id=after_id, limit=limit)

    def rename_terminal(
        self,
        *,
        terminal_id: UUID,
        expected_name_version: int,
        display_name: str,
        correlation_id: UUID,
    ) -> TerminalRecord:
        name = display_name.strip()
        if not 1 <= len(name) <= 120:
            raise InvalidRequestError(
                "invalid_terminal_name", "Terminal name must be 1..120 characters"
            )
        now = self._now()
        with self._uow_factory() as uow:
            terminal = uow.terminals.get_active(terminal_id, for_update=True)
            if terminal is None:
                raise NotFoundError("terminal")
            if terminal.name_version != expected_name_version:
                raise ConflictError("stale_terminal", "Terminal changed concurrently")
            updated = uow.terminals.rename(terminal_id, expected_name_version, name)
            if updated is None:
                raise ConflictError("stale_terminal", "Terminal changed concurrently")
            record_resource_event(
                uow,
                event_type="terminal.display_name_changed.v1",
                aggregate_type="terminal",
                aggregate_id=terminal_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={"terminal_id": str(terminal_id), "row_version": updated.row_version},
                action="terminal.display_name.update",
                actor_type="operator",
                actor_id=None,
            )
            uow.commit()
        return updated

    def current_capability(self, terminal_id: UUID) -> CapabilityProfileRecord | None:
        with self._uow_factory() as uow:
            terminal = uow.terminals.get_active(terminal_id)
            if terminal is None:
                raise NotFoundError("terminal")
            if terminal.current_capability_profile_id is None:
                return None
            profile = uow.capabilities.get(terminal.current_capability_profile_id)
            if profile is None or profile.terminal_id != terminal_id:
                raise ConflictError("capability_unavailable", "Capability profile unavailable")
            return profile

    def require_linux_terminal(self, terminal_id: UUID) -> None:
        with self._uow_factory() as uow:
            terminal = uow.terminals.get_active(terminal_id)
            if terminal is None:
                raise NotFoundError("terminal")
            if terminal.terminal_type is not TerminalType.LINUX:
                raise InvalidRequestError("linux_terminal_required", "Linux terminal required")

    def list_terminal_management(
        self, *, after_id: UUID | None = None, limit: int = 50
    ) -> list[TerminalManagementRecord]:
        with self._uow_factory() as uow:
            facts = uow.terminals.list_management_facts(
                after_id=after_id, limit=limit, now=self._now()
            )
        return [self._terminal_management_record(item) for item in facts]

    def delete_terminal(
        self, *, terminal_id: UUID, expected_version: int, correlation_id: UUID
    ) -> None:
        now = self._now()
        with self._uow_factory() as uow:
            terminal = uow.terminals.get_active(terminal_id, for_update=True)
            if terminal is None:
                raise NotFoundError("terminal")
            if terminal.service_status is not TerminalServiceStatus.OFFLINE:
                raise ConflictError(
                    "terminal_not_offline", "Terminal must be offline before deletion"
                )
            if terminal.row_version != expected_version:
                raise ConflictError("stale_terminal", "Terminal version is stale")
            uow.leases.release_expired(terminal_id=terminal_id, target_device_id=None, now=now)
            if uow.leases.has_active(terminal_id=terminal_id, now=now):
                raise ConflictError("terminal_in_use", "Terminal has an active lease")
            if uow.target_devices.has_managed_by(terminal_id):
                raise ConflictError(
                    "terminal_manages_target_devices",
                    "Reassign target devices before deleting this terminal",
                )
            if not uow.terminals.soft_delete(terminal_id, expected_version, now):
                raise ConflictError("stale_terminal", "Terminal changed concurrently")
            uow.credentials.revoke_active(terminal_id, now)
            record_resource_event(
                uow,
                event_type="terminal.deleted.v1",
                aggregate_type="terminal",
                aggregate_id=terminal_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={"terminal_id": str(terminal_id)},
                action="terminal.delete",
                actor_type="operator",
                actor_id=None,
            )
            uow.commit()

    @staticmethod
    def _terminal_management_record(
        facts: TerminalManagementFacts,
    ) -> TerminalManagementRecord:
        terminal = facts.terminal
        if terminal.service_status is not TerminalServiceStatus.OFFLINE:
            availability = ActionAvailability(
                allowed=False,
                refusal_code="terminal_not_offline",
                refusal_message="Terminal must be offline before deletion",
            )
        elif facts.has_active_lease:
            availability = ActionAvailability(
                allowed=False,
                refusal_code="terminal_in_use",
                refusal_message="Terminal has an active lease",
            )
        elif facts.manages_target_devices:
            availability = ActionAvailability(
                allowed=False,
                refusal_code="terminal_manages_target_devices",
                refusal_message="Reassign target devices before deleting this terminal",
            )
        else:
            availability = ActionAvailability(allowed=True)
        return TerminalManagementRecord(terminal=terminal, delete=availability)
