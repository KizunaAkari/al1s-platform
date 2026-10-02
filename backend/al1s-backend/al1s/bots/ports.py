from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Any, Protocol
from uuid import UUID

from al1s.bots.types import (
    BotConfigApplicationRecord,
    BotConfigApplicationStatus,
    BotConfigVersionRecord,
    BotHealthReportRecord,
    BotHealthStatus,
    BotRegistrationGrantRecord,
    BotServiceRecord,
    BotWorkerIdentityRecord,
)
from al1s.kernel.ports import AuditRepository, OutboxRepository
from al1s.secrets.ports import SecretRepository


class BotServiceRepository(Protocol):
    def add(self, record: BotServiceRecord) -> bool: ...
    def get(self, service_id: UUID, *, for_update: bool = False) -> BotServiceRecord | None: ...
    def list_active(self, *, after_id: UUID | None, limit: int) -> list[BotServiceRecord]: ...
    def set_desired(
        self, service_id: UUID, expected_version: int, config_version_id: UUID, now: datetime
    ) -> BotServiceRecord | None: ...
    def set_applied_if_desired(
        self, service_id: UUID, config_version_id: UUID, now: datetime
    ) -> BotServiceRecord | None: ...
    def disable(
        self, service_id: UUID, expected_version: int, now: datetime
    ) -> BotServiceRecord | None: ...
    def bump_version(
        self, service_id: UUID, expected_version: int, now: datetime
    ) -> BotServiceRecord | None: ...


class BotRegistrationGrantRepository(Protocol):
    def add(self, record: BotRegistrationGrantRecord) -> None: ...
    def get(
        self, grant_id: UUID, *, for_update: bool = False
    ) -> BotRegistrationGrantRecord | None: ...
    def consume(
        self, grant_id: UUID, expected_version: int, identity_id: UUID, now: datetime
    ) -> bool: ...


class BotWorkerIdentityRepository(Protocol):
    def add(self, record: BotWorkerIdentityRecord) -> None: ...
    def get(
        self, identity_id: UUID, *, for_update: bool = False
    ) -> BotWorkerIdentityRecord | None: ...
    def get_for_service(
        self, service_id: UUID, *, for_update: bool = False
    ) -> BotWorkerIdentityRecord | None: ...
    def replace_credential(
        self, identity_id: UUID, credential_digest: str, credential_version: int, now: datetime
    ) -> bool: ...
    def touch(self, identity_id: UUID, now: datetime) -> None: ...
    def disable_for_service(self, service_id: UUID, now: datetime) -> int: ...


class BotConfigRepository(Protocol):
    def next_version_no(self, service_id: UUID) -> int: ...
    def add_version(self, record: BotConfigVersionRecord) -> None: ...
    def get_version(self, config_version_id: UUID) -> BotConfigVersionRecord | None: ...
    def add_application(self, record: BotConfigApplicationRecord) -> None: ...
    def get_application(
        self, application_id: UUID, *, for_update: bool = False
    ) -> BotConfigApplicationRecord | None: ...
    def get_pending_for_service(self, service_id: UUID) -> BotConfigApplicationRecord | None: ...
    def list_applications(
        self,
        service_id: UUID,
        *,
        before_requested_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[BotConfigApplicationRecord]: ...
    def get_application_by_receipt(
        self, receipt_event_id: UUID
    ) -> BotConfigApplicationRecord | None: ...
    def settle_application(
        self,
        application_id: UUID,
        expected_version: int,
        status: BotConfigApplicationStatus,
        worker_instance_id: str,
        error_code: str | None,
        error_summary: str | None,
        receipt_event_id: UUID,
        now: datetime,
    ) -> BotConfigApplicationRecord | None: ...
    def delete_settled_applications_before(self, cutoff: datetime, *, limit: int) -> int: ...
    def delete_unreferenced_versions_before(
        self, cutoff: datetime, *, limit: int
    ) -> list[UUID | None]: ...


class BotHealthRepository(Protocol):
    def add(
        self,
        *,
        report_id: UUID,
        receipt_event_id: UUID,
        service_id: UUID,
        identity_id: UUID,
        config_version_id: UUID | None,
        status: BotHealthStatus,
        diagnostics: dict[str, Any],
        reported_at: datetime,
    ) -> bool: ...
    def get_by_receipt(self, receipt_event_id: UUID) -> BotHealthReportRecord | None: ...
    def list_for_service(
        self,
        service_id: UUID,
        *,
        before_reported_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[BotHealthReportRecord]: ...
    def delete_before(self, cutoff: datetime, *, limit: int) -> int: ...


class BotUnitOfWork(Protocol):
    services: BotServiceRepository
    grants: BotRegistrationGrantRepository
    identities: BotWorkerIdentityRepository
    configs: BotConfigRepository
    health: BotHealthRepository
    secrets: SecretRepository
    audit: AuditRepository
    outbox: OutboxRepository

    def __enter__(self) -> BotUnitOfWork: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def flush(self) -> None: ...
