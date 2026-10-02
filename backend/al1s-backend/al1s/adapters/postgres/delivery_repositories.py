from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from al1s.adapters.postgres.delivery_models import (
    CommandAcknowledgementRow,
    OfflineStartPermitRow,
    TaskPackageRow,
    TerminalCommandRow,
    TerminalReportRow,
)
from al1s.adapters.postgres.scheduling_models import ExecutionRow
from al1s.execution.delivery_types import (
    CancellationAcknowledgementOutcome,
    CommandAcknowledgementRecord,
    CommandKind,
    CommandStatus,
    PackageStatus,
    RejectionDisposition,
    TaskPackageRecord,
    TerminalCommandRecord,
    TerminalReportDisposition,
    TerminalReportKind,
    TerminalReportRecord,
)
from al1s.execution.offline_permits import OfflinePermitStatus, OfflineStartPermitRecord

MAX_COMMAND_BATCH = 50


def _package_record(row: TaskPackageRow) -> TaskPackageRecord:
    return TaskPackageRecord(
        package_id=row.id,
        attempt_id=row.attempt_id,
        execution_id=row.execution_id,
        snapshot_id=row.snapshot_id,
        terminal_id=row.terminal_id,
        target_device_id=row.target_device_id,
        protocol_version=row.protocol_version,
        package_schema_version=row.package_schema_version,
        snapshot_schema_version=row.snapshot_schema_version,
        package_hash=row.package_hash,
        manifest=dict(row.manifest),
        status=PackageStatus(row.status),
        delivery_attempt_count=row.delivery_attempt_count,
        next_delivery_at=row.next_delivery_at,
        last_rejection_disposition=(
            RejectionDisposition(row.last_rejection_disposition)
            if row.last_rejection_disposition
            else None
        ),
        last_rejection_code=row.last_rejection_code,
        last_rejection_diagnostic=row.last_rejection_diagnostic,
        accepted_at=row.accepted_at,
        failed_at=row.failed_at,
        created_at=row.created_at,
        row_version=row.row_version,
    )


def _command_record(row: TerminalCommandRow) -> TerminalCommandRecord:
    return TerminalCommandRecord(
        command_id=row.id,
        terminal_id=row.terminal_id,
        command_kind=CommandKind(row.command_kind),
        package_id=row.package_id,
        attempt_id=row.attempt_id,
        delivery_no=row.delivery_no,
        status=CommandStatus(row.status),
        payload=dict(row.payload),
        available_at=row.available_at,
        acknowledged_at=row.acknowledged_at,
        created_at=row.created_at,
        row_version=row.row_version,
    )


def _report_record(row: TerminalReportRow) -> TerminalReportRecord:
    return TerminalReportRecord(
        terminal_id=row.terminal_id,
        report_id=row.report_id,
        report_kind=TerminalReportKind(row.report_kind),
        disposition=TerminalReportDisposition(row.disposition),
        command_id=row.command_id,
        attempt_id=row.attempt_id,
        package_id=row.package_id,
        lease_id=row.lease_id,
        error_code=row.error_code,
        diagnostic=row.diagnostic,
        result_diagnostic=row.result_diagnostic,
        payload_hash=row.payload_hash,
        occurred_at=row.occurred_at,
        received_at=row.received_at,
    )


def _offline_permit_record(row: OfflineStartPermitRow) -> OfflineStartPermitRecord:
    return OfflineStartPermitRecord(
        permit_id=row.id,
        attempt_id=row.attempt_id,
        package_id=row.package_id,
        execution_id=row.execution_id,
        terminal_id=row.terminal_id,
        target_device_id=row.target_device_id,
        package_hash=row.package_hash,
        permit_version=row.permit_version,
        token_digest=row.token_digest,
        status=OfflinePermitStatus(row.status),
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
        consumed_start_report_id=row.consumed_start_report_id,
        revoked_at=row.revoked_at,
        row_version=row.row_version,
    )


def _command_acknowledgement_record(
    row: CommandAcknowledgementRow,
) -> CommandAcknowledgementRecord:
    return CommandAcknowledgementRecord(
        terminal_id=row.terminal_id,
        report_id=row.report_id,
        command_id=row.command_id,
        attempt_id=row.attempt_id,
        execution_id=row.execution_id,
        outcome=CancellationAcknowledgementOutcome(row.outcome),
        payload_hash=row.payload_hash,
        occurred_at=row.occurred_at,
        received_at=row.received_at,
    )


class PostgresCommandAcknowledgementRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, terminal_id: UUID, report_id: UUID) -> CommandAcknowledgementRecord | None:
        row = self._session.get(CommandAcknowledgementRow, (terminal_id, report_id))
        return _command_acknowledgement_record(row) if row else None

    def add(self, acknowledgement: CommandAcknowledgementRecord) -> None:
        self._session.add(
            CommandAcknowledgementRow(
                terminal_id=acknowledgement.terminal_id,
                report_id=acknowledgement.report_id,
                command_id=acknowledgement.command_id,
                attempt_id=acknowledgement.attempt_id,
                execution_id=acknowledgement.execution_id,
                outcome=acknowledgement.outcome.value,
                payload_hash=acknowledgement.payload_hash,
                occurred_at=acknowledgement.occurred_at,
                received_at=acknowledgement.received_at,
            )
        )
        self._session.flush()


class PostgresTaskPackageRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, package: TaskPackageRecord) -> None:
        self._session.add(
            TaskPackageRow(
                id=package.package_id,
                attempt_id=package.attempt_id,
                execution_id=package.execution_id,
                snapshot_id=package.snapshot_id,
                terminal_id=package.terminal_id,
                target_device_id=package.target_device_id,
                protocol_version=package.protocol_version,
                package_schema_version=package.package_schema_version,
                snapshot_schema_version=package.snapshot_schema_version,
                package_hash=package.package_hash,
                manifest=dict(package.manifest),
                status=package.status.value,
                delivery_attempt_count=package.delivery_attempt_count,
                next_delivery_at=package.next_delivery_at,
                last_rejection_disposition=(
                    package.last_rejection_disposition.value
                    if package.last_rejection_disposition
                    else None
                ),
                last_rejection_code=package.last_rejection_code,
                last_rejection_diagnostic=package.last_rejection_diagnostic,
                accepted_at=package.accepted_at,
                failed_at=package.failed_at,
                created_at=package.created_at,
                row_version=package.row_version,
            )
        )

    def execution_route_for_package(self, package_id: UUID) -> tuple[UUID, UUID] | None:
        # Scalar routing lookup leaves the ORM identity map empty before row locks.
        route = self._session.execute(
            select(ExecutionRow.task_request_id, TaskPackageRow.execution_id)
            .join(TaskPackageRow, TaskPackageRow.execution_id == ExecutionRow.id)
            .where(TaskPackageRow.id == package_id)
        ).one_or_none()
        return (route[0], route[1]) if route is not None else None

    def get(self, package_id: UUID) -> TaskPackageRecord | None:
        row = self._session.get(TaskPackageRow, package_id)
        return _package_record(row) if row else None

    def get_for_update(self, package_id: UUID) -> TaskPackageRecord | None:
        row = self._session.scalar(
            select(TaskPackageRow).where(TaskPackageRow.id == package_id).with_for_update()
        )
        return _package_record(row) if row else None

    def get_by_attempt_for_update(self, attempt_id: UUID) -> TaskPackageRecord | None:
        row = self._session.scalar(
            select(TaskPackageRow).where(TaskPackageRow.attempt_id == attempt_id).with_for_update()
        )
        return _package_record(row) if row else None

    def set_accepted(
        self, package_id: UUID, expected_version: int, accepted_at: datetime
    ) -> TaskPackageRecord | None:
        row = self._session.execute(
            update(TaskPackageRow)
            .where(
                TaskPackageRow.id == package_id,
                TaskPackageRow.row_version == expected_version,
                TaskPackageRow.status == PackageStatus.AVAILABLE.value,
            )
            .values(
                status=PackageStatus.ACCEPTED.value,
                accepted_at=accepted_at,
                row_version=TaskPackageRow.row_version + 1,
            )
            .returning(TaskPackageRow)
        ).scalar_one_or_none()
        return _package_record(row) if row else None

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
    ) -> TaskPackageRecord | None:
        row = self._session.execute(
            update(TaskPackageRow)
            .where(
                TaskPackageRow.id == package_id,
                TaskPackageRow.row_version == expected_version,
                TaskPackageRow.status == PackageStatus.AVAILABLE.value,
            )
            .values(
                status=status.value,
                delivery_attempt_count=TaskPackageRow.delivery_attempt_count + 1,
                next_delivery_at=next_delivery_at,
                last_rejection_disposition=disposition.value,
                last_rejection_code=code,
                last_rejection_diagnostic=diagnostic,
                failed_at=failed_at,
                row_version=TaskPackageRow.row_version + 1,
            )
            .returning(TaskPackageRow)
        ).scalar_one_or_none()
        return _package_record(row) if row else None

    def prepare_blocked_redelivery(
        self,
        package_id: UUID,
        expected_version: int,
        *,
        next_delivery_at: datetime,
    ) -> TaskPackageRecord | None:
        row = self._session.execute(
            update(TaskPackageRow)
            .where(
                TaskPackageRow.id == package_id,
                TaskPackageRow.row_version == expected_version,
                TaskPackageRow.status == PackageStatus.AVAILABLE.value,
                TaskPackageRow.last_rejection_disposition
                == RejectionDisposition.BLOCKED_WAIT.value,
            )
            .values(
                next_delivery_at=next_delivery_at,
                row_version=TaskPackageRow.row_version + 1,
            )
            .returning(TaskPackageRow)
        ).scalar_one_or_none()
        return _package_record(row) if row else None

    def cancel(
        self,
        package_id: UUID,
        expected_version: int,
        *,
        cancelled_at: datetime,
    ) -> TaskPackageRecord | None:
        row = self._session.execute(
            update(TaskPackageRow)
            .where(
                TaskPackageRow.id == package_id,
                TaskPackageRow.row_version == expected_version,
                TaskPackageRow.status == PackageStatus.ACCEPTED.value,
            )
            .values(
                status=PackageStatus.CANCELLED.value,
                failed_at=cancelled_at,
                row_version=TaskPackageRow.row_version + 1,
            )
            .returning(TaskPackageRow)
        ).scalar_one_or_none()
        return _package_record(row) if row else None


class PostgresTerminalCommandRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, command: TerminalCommandRecord) -> None:
        self._session.add(
            TerminalCommandRow(
                id=command.command_id,
                terminal_id=command.terminal_id,
                command_kind=command.command_kind.value,
                package_id=command.package_id,
                attempt_id=command.attempt_id,
                delivery_no=command.delivery_no,
                status=command.status.value,
                payload=dict(command.payload),
                available_at=command.available_at,
                acknowledged_at=command.acknowledged_at,
                created_at=command.created_at,
                row_version=command.row_version,
            )
        )

    def get_for_update(self, command_id: UUID) -> TerminalCommandRecord | None:
        row = self._session.scalar(
            select(TerminalCommandRow).where(TerminalCommandRow.id == command_id).with_for_update()
        )
        return _command_record(row) if row else None

    def list_pending(
        self, terminal_id: UUID, *, now: datetime, limit: int
    ) -> list[TerminalCommandRecord]:
        if not 1 <= limit <= MAX_COMMAND_BATCH:
            raise ValueError(f"limit must be between 1 and {MAX_COMMAND_BATCH}")
        rows = self._session.scalars(
            select(TerminalCommandRow)
            .where(
                TerminalCommandRow.terminal_id == terminal_id,
                TerminalCommandRow.status == CommandStatus.PENDING.value,
                TerminalCommandRow.available_at <= now,
            )
            .order_by(
                TerminalCommandRow.available_at,
                TerminalCommandRow.created_at,
                TerminalCommandRow.id,
            )
            .limit(limit)
        ).all()
        return [_command_record(row) for row in rows]

    def acknowledge(
        self, command_id: UUID, expected_version: int, acknowledged_at: datetime
    ) -> TerminalCommandRecord | None:
        row = self._session.execute(
            update(TerminalCommandRow)
            .where(
                TerminalCommandRow.id == command_id,
                TerminalCommandRow.row_version == expected_version,
                TerminalCommandRow.status == CommandStatus.PENDING.value,
            )
            .values(
                status=CommandStatus.ACKNOWLEDGED.value,
                acknowledged_at=acknowledged_at,
                row_version=TerminalCommandRow.row_version + 1,
            )
            .returning(TerminalCommandRow)
        ).scalar_one_or_none()
        return _command_record(row) if row else None

    def supersede_pending(
        self,
        attempt_id: UUID,
        *,
        acknowledged_at: datetime,
        exclude_command_ids: Sequence[UUID] = (),
    ) -> int:
        statement = update(TerminalCommandRow).where(
            TerminalCommandRow.attempt_id == attempt_id,
            TerminalCommandRow.status == CommandStatus.PENDING.value,
        )
        if exclude_command_ids:
            statement = statement.where(TerminalCommandRow.id.not_in(exclude_command_ids))
        rows = self._session.execute(
            statement.values(
                status=CommandStatus.SUPERSEDED.value,
                acknowledged_at=acknowledged_at,
                row_version=TerminalCommandRow.row_version + 1,
            ).returning(TerminalCommandRow.id)
        ).scalars()
        return len(rows.all())


class PostgresTerminalReportRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_idempotency_key(self, terminal_id: UUID, report_id: UUID) -> None:
        digest = hashlib.sha256(terminal_id.bytes + report_id.bytes).digest()
        lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
        )

    def get(self, terminal_id: UUID, report_id: UUID) -> TerminalReportRecord | None:
        row = self._session.get(TerminalReportRow, (terminal_id, report_id))
        return _report_record(row) if row else None

    def add(self, report: TerminalReportRecord) -> None:
        self._session.add(
            TerminalReportRow(
                terminal_id=report.terminal_id,
                report_id=report.report_id,
                report_kind=report.report_kind.value,
                disposition=report.disposition.value,
                command_id=report.command_id,
                attempt_id=report.attempt_id,
                package_id=report.package_id,
                lease_id=report.lease_id,
                error_code=report.error_code,
                diagnostic=report.diagnostic,
                result_diagnostic=report.result_diagnostic,
                payload_hash=report.payload_hash,
                occurred_at=report.occurred_at,
                received_at=report.received_at,
            )
        )
        if report.report_kind.value == "attempt_result" and report.disposition.value == "accepted":
            from al1s.adapters.postgres.lineup_projection import refresh_for_attempt

            refresh_for_attempt(self._session, report.attempt_id)


class PostgresOfflineStartPermitRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, permit: OfflineStartPermitRecord) -> None:
        self._session.add(
            OfflineStartPermitRow(
                id=permit.permit_id,
                attempt_id=permit.attempt_id,
                package_id=permit.package_id,
                execution_id=permit.execution_id,
                terminal_id=permit.terminal_id,
                target_device_id=permit.target_device_id,
                package_hash=permit.package_hash,
                permit_version=permit.permit_version,
                token_digest=permit.token_digest,
                status=permit.status.value,
                issued_at=permit.issued_at,
                expires_at=permit.expires_at,
                consumed_at=permit.consumed_at,
                consumed_start_report_id=permit.consumed_start_report_id,
                revoked_at=permit.revoked_at,
                row_version=permit.row_version,
            )
        )

    def get_by_attempt_for_update(self, attempt_id: UUID) -> OfflineStartPermitRecord | None:
        row = self._session.scalar(
            select(OfflineStartPermitRow)
            .where(OfflineStartPermitRow.attempt_id == attempt_id)
            .with_for_update()
        )
        return _offline_permit_record(row) if row else None

    def consume(
        self,
        permit_id: UUID,
        expected_version: int,
        *,
        start_report_id: UUID,
        consumed_at: datetime,
    ) -> OfflineStartPermitRecord | None:
        row = self._session.execute(
            update(OfflineStartPermitRow)
            .where(
                OfflineStartPermitRow.id == permit_id,
                OfflineStartPermitRow.row_version == expected_version,
                OfflineStartPermitRow.status == OfflinePermitStatus.ISSUED.value,
            )
            .values(
                status=OfflinePermitStatus.CONSUMED.value,
                consumed_at=consumed_at,
                consumed_start_report_id=start_report_id,
                row_version=OfflineStartPermitRow.row_version + 1,
            )
            .returning(OfflineStartPermitRow)
        ).scalar_one_or_none()
        return _offline_permit_record(row) if row else None

    def revoke(
        self,
        permit_id: UUID,
        expected_version: int,
        *,
        revoked_at: datetime,
    ) -> OfflineStartPermitRecord | None:
        row = self._session.execute(
            update(OfflineStartPermitRow)
            .where(
                OfflineStartPermitRow.id == permit_id,
                OfflineStartPermitRow.row_version == expected_version,
                OfflineStartPermitRow.status == OfflinePermitStatus.ISSUED.value,
            )
            .values(
                status=OfflinePermitStatus.REVOKED.value,
                revoked_at=revoked_at,
                row_version=OfflineStartPermitRow.row_version + 1,
            )
            .returning(OfflineStartPermitRow)
        ).scalar_one_or_none()
        return _offline_permit_record(row) if row else None
