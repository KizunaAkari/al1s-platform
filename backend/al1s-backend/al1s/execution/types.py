from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from al1s.execution.storage import StorageObservation


class TerminalType(StrEnum):
    LINUX = "linux"
    ANDROID = "android"


class TerminalServiceStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


class TerminalAcceptanceStatus(StrEnum):
    ACCEPTING = "accepting"
    DRAINING = "draining"
    DISABLED = "disabled"


class TargetDeviceMode(StrEnum):
    UNASSIGNED = "unassigned"
    STANDALONE = "standalone"
    MOUNTED = "mounted"


class TargetIdentifierSource(StrEnum):
    APK_INSTALLATION = "apk_installation"
    ADB_SERIAL = "adb_serial"
    ANDROID_ID = "android_id"


class LeaseKind(StrEnum):
    EXECUTION = "execution"
    REMOTE_CONTROL = "remote_control"
    QUICK_TEST = "quick_test"
    MAINTENANCE = "maintenance"


class LeaseOwnerKind(StrEnum):
    EXECUTION = "execution"
    EXECUTION_ATTEMPT = "execution_attempt"
    REMOTE_SESSION = "remote_session"
    QUICK_TEST = "quick_test"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class RegistrationGrant:
    grant_id: UUID
    secret_digest: str
    allowed_terminal_type: TerminalType | None
    target_device_id: UUID | None
    expires_at: datetime
    consumed_at: datetime | None
    consumed_by_terminal_id: UUID | None
    row_version: int


@dataclass(frozen=True, slots=True)
class IssuedRegistrationGrant:
    grant_id: UUID
    registration_code: str
    expires_at: datetime
    target_device_id: UUID | None


@dataclass(frozen=True, slots=True)
class TerminalRecord:
    terminal_id: UUID
    installation_id: UUID
    terminal_type: TerminalType
    display_name: str
    service_status: TerminalServiceStatus
    acceptance_status: TerminalAcceptanceStatus
    agent_version: str
    current_capability_profile_id: UUID | None
    row_version: int
    created_at: datetime
    last_seen_at: datetime | None
    deleted_at: datetime | None
    storage: StorageObservation | None = None
    storage_observed_at: datetime | None = None
    storage_probe_ok: bool = False
    storage_alert_active: bool = False
    name_is_custom: bool = False
    name_version: int = 0


@dataclass(frozen=True, slots=True)
class TerminalManagementFacts:
    terminal: TerminalRecord
    has_active_lease: bool
    manages_target_devices: bool


@dataclass(frozen=True, slots=True)
class ActionAvailability:
    allowed: bool
    refusal_code: str | None = None
    refusal_message: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalManagementRecord:
    terminal: TerminalRecord
    delete: ActionAvailability


@dataclass(frozen=True, slots=True)
class RegisteredTerminal:
    terminal: TerminalRecord
    credential: str
    target_device: TargetDeviceRecord | None = None


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    schema_version: int
    protocol_version: int
    agent_version: str
    os_name: str
    os_version: str
    architecture: str
    cpu_cores: int
    memory_bytes: int
    storage_available_bytes: int
    accelerator_type: str | None
    low_resource: bool
    provider_keys: tuple[str, ...]
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CapabilityProfileRecord:
    profile_id: UUID
    terminal_id: UUID
    revision: int
    manifest_hash: str
    manifest: CapabilityManifest
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TargetDeviceRecord:
    device_id: UUID
    display_name: str
    platform: str
    mode: TargetDeviceMode
    managing_terminal_id: UUID | None
    row_version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    availability: str = "unknown"
    availability_reason: str | None = "not_observed"
    availability_observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TargetIdentifierRecord:
    identifier_id: UUID
    target_device_id: UUID | None
    source_terminal_id: UUID
    source_type: TargetIdentifierSource
    identifier_digest: str
    display_hint: str
    row_version: int
    created_at: datetime
    bound_at: datetime | None
    deleted_at: datetime | None
    adb_state: str | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ExecutionLeaseRecord:
    lease_id: UUID
    terminal_id: UUID | None
    target_device_id: UUID | None
    lease_kind: LeaseKind
    owner_kind: LeaseOwnerKind
    owner_id: UUID
    acquired_at: datetime
    expires_at: datetime
    released_at: datetime | None
    row_version: int
