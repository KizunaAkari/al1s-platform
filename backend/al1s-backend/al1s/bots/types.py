from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class BotServiceKind(StrEnum):
    ONEBOT_GATEWAY = "onebot_gateway"
    DISCORD_BRIDGE = "discord_bridge"


class BotConfigApplicationStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"


class BotHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class BotServiceRecord:
    service_id: UUID
    kind: BotServiceKind
    name: str
    enabled: bool
    desired_config_version_id: UUID | None
    applied_config_version_id: UUID | None
    row_version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class BotRegistrationGrantRecord:
    grant_id: UUID
    service_id: UUID
    secret_digest: str
    expires_at: datetime
    consumed_at: datetime | None
    consumed_by_identity_id: UUID | None
    row_version: int


@dataclass(frozen=True, slots=True)
class IssuedBotRegistrationGrant:
    grant_id: UUID
    registration_code: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class BotWorkerIdentityRecord:
    identity_id: UUID
    service_id: UUID
    credential_digest: str
    credential_version: int
    enabled: bool
    created_at: datetime
    rotated_at: datetime | None
    last_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class RegisteredBotWorker:
    identity_id: UUID
    service_id: UUID
    credential: str


@dataclass(frozen=True, slots=True)
class BotConfigVersionRecord:
    config_version_id: UUID
    service_id: UUID
    version_no: int
    settings: dict[str, Any]
    secret_id: UUID | None
    onebot_service_id: UUID | None
    config_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BotConfigApplicationRecord:
    application_id: UUID
    service_id: UUID
    config_version_id: UUID
    status: BotConfigApplicationStatus
    worker_instance_id: str | None
    error_code: str | None
    error_summary: str | None
    receipt_event_id: UUID | None
    requested_at: datetime
    completed_at: datetime | None
    row_version: int


@dataclass(frozen=True, slots=True)
class BotWorkerConfig:
    application: BotConfigApplicationRecord
    version: BotConfigVersionRecord
    secret: str | None
    linked_onebot: BotLinkedRuntimeConfig | None


@dataclass(frozen=True, slots=True)
class BotLinkedRuntimeConfig:
    service_id: UUID
    config_version_id: UUID
    settings: dict[str, Any]
    secret: str | None


@dataclass(frozen=True, slots=True)
class SubmittedBotConfig:
    service: BotServiceRecord
    version: BotConfigVersionRecord
    application: BotConfigApplicationRecord


@dataclass(frozen=True, slots=True)
class BotHealthReportRecord:
    report_id: UUID
    receipt_event_id: UUID
    service_id: UUID
    identity_id: UUID
    config_version_id: UUID | None
    status: BotHealthStatus
    diagnostics: dict[str, Any]
    reported_at: datetime


@dataclass(frozen=True, slots=True)
class BotHistoryCleanupResult:
    deleted_health_reports: int
    deleted_applications: int
    deleted_config_versions: int
    deleted_secrets: int
