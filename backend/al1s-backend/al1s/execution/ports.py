from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Protocol
from uuid import UUID

from al1s.execution.storage import StorageObservation
from al1s.execution.types import (
    CapabilityManifest,
    CapabilityProfileRecord,
    ExecutionLeaseRecord,
    LeaseKind,
    LeaseOwnerKind,
    RegistrationGrant,
    TargetDeviceMode,
    TargetDeviceRecord,
    TargetIdentifierRecord,
    TargetIdentifierSource,
    TerminalAcceptanceStatus,
    TerminalManagementFacts,
    TerminalRecord,
    TerminalServiceStatus,
    TerminalType,
)
from al1s.kernel.ports import AuditRepository, OutboxRepository


class RegistrationGrantRepository(Protocol):
    def add(
        self,
        grant_id: UUID,
        secret_digest: str,
        allowed_terminal_type: TerminalType | None,
        target_device_id: UUID | None,
        expires_at: datetime,
    ) -> None: ...

    def get_for_update(self, grant_id: UUID) -> RegistrationGrant | None: ...

    def consume(
        self,
        grant_id: UUID,
        expected_version: int,
        terminal_id: UUID,
        consumed_at: datetime,
    ) -> bool: ...


class TerminalRepository(Protocol):
    def expire_heartbeats(self, cutoff: datetime, *, limit: int) -> list[TerminalRecord]: ...

    def add(
        self,
        terminal_id: UUID,
        installation_id: UUID,
        terminal_type: TerminalType,
        display_name: str,
        agent_version: str,
        now: datetime,
    ) -> TerminalRecord: ...

    def find_active_by_installation_id_for_update(
        self, installation_id: UUID
    ) -> TerminalRecord | None: ...

    def get_active(
        self, terminal_id: UUID, *, for_update: bool = False
    ) -> TerminalRecord | None: ...

    def refresh_registration(
        self,
        terminal_id: UUID,
        expected_version: int,
        display_name: str,
        agent_version: str,
        now: datetime,
    ) -> TerminalRecord | None: ...

    def rename(
        self, terminal_id: UUID, expected_name_version: int, display_name: str
    ) -> TerminalRecord | None: ...

    def heartbeat(
        self,
        terminal_id: UUID,
        expected_version: int,
        service_status: TerminalServiceStatus,
        acceptance_status: TerminalAcceptanceStatus,
        agent_version: str,
        now: datetime,
        storage: StorageObservation | None = None,
        storage_alert_active: bool = False,
    ) -> TerminalRecord | None: ...

    def set_current_capability(
        self,
        terminal_id: UUID,
        expected_version: int,
        profile_id: UUID,
        agent_version: str,
    ) -> TerminalRecord | None: ...

    def soft_delete(
        self, terminal_id: UUID, expected_version: int, deleted_at: datetime
    ) -> bool: ...

    def list_active(self, *, after_id: UUID | None, limit: int) -> list[TerminalRecord]: ...

    def list_management_facts(
        self, *, after_id: UUID | None, limit: int, now: datetime
    ) -> list[TerminalManagementFacts]: ...


class TerminalCredentialRepository(Protocol):
    def get_active_digest(self, terminal_id: UUID) -> str | None: ...

    def revoke_active(self, terminal_id: UUID, revoked_at: datetime) -> int: ...

    def add(self, credential_id: UUID, terminal_id: UUID, secret_digest: str) -> None: ...


class CapabilityProfileRepository(Protocol):
    def get(self, profile_id: UUID) -> CapabilityProfileRecord | None: ...

    def find_revision(self, terminal_id: UUID, revision: int) -> CapabilityProfileRecord | None: ...

    def add(
        self,
        profile_id: UUID,
        terminal_id: UUID,
        revision: int,
        manifest_hash: str,
        manifest: CapabilityManifest,
    ) -> CapabilityProfileRecord: ...


class TargetDeviceRepository(Protocol):
    def has_pending_control(self, device_id: UUID, manager_id: UUID | None) -> bool: ...

    def add(self, device_id: UUID, display_name: str, now: datetime) -> TargetDeviceRecord: ...

    def get_active(
        self, device_id: UUID, *, for_update: bool = False
    ) -> TargetDeviceRecord | None: ...

    def rename(
        self, device_id: UUID, expected_version: int, display_name: str, now: datetime
    ) -> TargetDeviceRecord | None: ...

    def update_mode(
        self,
        device_id: UUID,
        expected_version: int,
        mode: TargetDeviceMode,
        managing_terminal_id: UUID | None,
        now: datetime,
    ) -> TargetDeviceRecord | None: ...

    def list_active(self, *, after_id: UUID | None, limit: int) -> list[TargetDeviceRecord]: ...

    def availability_rows(
        self, device_ids: tuple[UUID, ...]
    ) -> dict[UUID, tuple[str | None, str | None, datetime | None]]: ...

    def has_managed_by(self, terminal_id: UUID) -> bool: ...


class TargetIdentifierRepository(Protocol):
    def list_adb(
        self, *, terminal_id: UUID, after_id: UUID | None, limit: int
    ) -> list[TargetIdentifierRecord]: ...

    def find_active(
        self,
        source_type: TargetIdentifierSource,
        identifier_digest: str,
        *,
        for_update: bool = False,
    ) -> TargetIdentifierRecord | None: ...

    def find_bound_to_target(
        self,
        source_type: TargetIdentifierSource,
        target_device_id: UUID,
        *,
        for_update: bool = False,
    ) -> TargetIdentifierRecord | None: ...

    def get_active(
        self, identifier_id: UUID, *, for_update: bool = False
    ) -> TargetIdentifierRecord | None: ...

    def add(
        self,
        identifier_id: UUID,
        source_terminal_id: UUID,
        source_type: TargetIdentifierSource,
        identifier_digest: str,
        display_hint: str,
    ) -> TargetIdentifierRecord: ...

    def bind(
        self,
        identifier_id: UUID,
        expected_version: int,
        target_device_id: UUID,
        bound_at: datetime,
    ) -> TargetIdentifierRecord | None: ...

    def observe(
        self, identifier_id: UUID, adb_state: str, observed_at: datetime
    ) -> TargetIdentifierRecord: ...

    def observe_batch(
        self, terminal_id: UUID, items: tuple[tuple[UUID, str], ...], observed_at: datetime
    ) -> int: ...


class ExecutionLeaseRepository(Protocol):
    def release_expired(
        self,
        *,
        terminal_id: UUID | None,
        target_device_id: UUID | None,
        now: datetime,
    ) -> int: ...

    def has_active(
        self,
        *,
        terminal_id: UUID | None = None,
        target_device_id: UUID | None = None,
        now: datetime,
    ) -> bool: ...

    def add(
        self,
        lease_id: UUID,
        terminal_id: UUID | None,
        target_device_id: UUID | None,
        lease_kind: LeaseKind,
        owner_kind: LeaseOwnerKind,
        owner_id: UUID,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> ExecutionLeaseRecord: ...

    def get_for_update(self, lease_id: UUID) -> ExecutionLeaseRecord | None: ...

    def renew(
        self,
        lease_id: UUID,
        expected_version: int,
        owner_kind: LeaseOwnerKind,
        owner_id: UUID,
        expires_at: datetime,
    ) -> ExecutionLeaseRecord | None: ...

    def release(
        self,
        lease_id: UUID,
        expected_version: int,
        owner_kind: LeaseOwnerKind,
        owner_id: UUID,
        released_at: datetime,
    ) -> bool: ...


class ExecutionUnitOfWork(Protocol):
    grants: RegistrationGrantRepository
    terminals: TerminalRepository
    credentials: TerminalCredentialRepository
    capabilities: CapabilityProfileRepository
    target_devices: TargetDeviceRepository
    target_identifiers: TargetIdentifierRepository
    leases: ExecutionLeaseRepository
    outbox: OutboxRepository
    audit: AuditRepository

    def __enter__(self) -> ExecutionUnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
