from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from al1s.execution.delivery_types import (
    CommandAcknowledgementRecord,
    PackageStatus,
    RejectionDisposition,
    TaskPackageRecord,
    TerminalCommandRecord,
    TerminalReportRecord,
)
from al1s.execution.offline_permits import OfflineStartPermitRecord


class TaskPackageRepository(Protocol):
    def add(self, package: TaskPackageRecord) -> None: ...

    def execution_route_for_package(self, package_id: UUID) -> tuple[UUID, UUID] | None: ...

    def get(self, package_id: UUID) -> TaskPackageRecord | None: ...

    def get_for_update(self, package_id: UUID) -> TaskPackageRecord | None: ...

    def get_by_attempt_for_update(self, attempt_id: UUID) -> TaskPackageRecord | None: ...

    def set_accepted(
        self, package_id: UUID, expected_version: int, accepted_at: datetime
    ) -> TaskPackageRecord | None: ...

    def record_rejection(
        self,
        package_id: UUID,
        expected_version: int,
        *,
        disposition: RejectionDisposition,
        code: str,
        diagnostic: str | None,
        status: PackageStatus,
        next_delivery_at: datetime,
        failed_at: datetime | None,
    ) -> TaskPackageRecord | None: ...

    def prepare_blocked_redelivery(
        self,
        package_id: UUID,
        expected_version: int,
        *,
        next_delivery_at: datetime,
    ) -> TaskPackageRecord | None: ...

    def cancel(
        self,
        package_id: UUID,
        expected_version: int,
        *,
        cancelled_at: datetime,
    ) -> TaskPackageRecord | None: ...


class TerminalCommandRepository(Protocol):
    def add(self, command: TerminalCommandRecord) -> None: ...

    def get_for_update(self, command_id: UUID) -> TerminalCommandRecord | None: ...

    def list_pending(
        self, terminal_id: UUID, *, now: datetime, limit: int
    ) -> list[TerminalCommandRecord]: ...

    def acknowledge(
        self, command_id: UUID, expected_version: int, acknowledged_at: datetime
    ) -> TerminalCommandRecord | None: ...

    def supersede_pending(
        self,
        attempt_id: UUID,
        *,
        acknowledged_at: datetime,
        exclude_command_ids: Sequence[UUID] = (),
    ) -> int: ...


class TerminalReportRepository(Protocol):
    def lock_idempotency_key(self, terminal_id: UUID, report_id: UUID) -> None: ...

    def get(self, terminal_id: UUID, report_id: UUID) -> TerminalReportRecord | None: ...

    def add(self, report: TerminalReportRecord) -> None: ...


class CommandAcknowledgementRepository(Protocol):
    def get(
        self, terminal_id: UUID, report_id: UUID
    ) -> CommandAcknowledgementRecord | None: ...

    def add(self, acknowledgement: CommandAcknowledgementRecord) -> None: ...


class OfflineStartPermitRepository(Protocol):
    def add(self, permit: OfflineStartPermitRecord) -> None: ...

    def get_by_attempt_for_update(self, attempt_id: UUID) -> OfflineStartPermitRecord | None: ...

    def consume(
        self,
        permit_id: UUID,
        expected_version: int,
        *,
        start_report_id: UUID,
        consumed_at: datetime,
    ) -> OfflineStartPermitRecord | None: ...

    def revoke(
        self,
        permit_id: UUID,
        expected_version: int,
        *,
        revoked_at: datetime,
    ) -> OfflineStartPermitRecord | None: ...
