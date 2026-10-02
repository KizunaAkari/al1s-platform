from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, exists, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session, aliased

from al1s.adapters.postgres.bot_models import (
    BotConfigApplicationRow,
    BotConfigVersionRow,
    BotHealthReportRow,
    BotRegistrationGrantRow,
    BotServiceRow,
    BotWorkerIdentityRow,
)
from al1s.bots.types import (
    BotConfigApplicationRecord,
    BotConfigApplicationStatus,
    BotConfigVersionRecord,
    BotHealthReportRecord,
    BotHealthStatus,
    BotRegistrationGrantRecord,
    BotServiceKind,
    BotServiceRecord,
    BotWorkerIdentityRecord,
)


class PostgresBotServiceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: BotServiceRecord) -> bool:
        result = self._session.execute(
            postgres_insert(BotServiceRow)
            .values(
                id=record.service_id,
                kind=record.kind.value,
                name=record.name,
                enabled=record.enabled,
                row_version=record.row_version,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            .on_conflict_do_nothing()
            .returning(BotServiceRow.id)
        ).scalar_one_or_none()
        return result is not None

    def get(self, service_id: UUID, *, for_update: bool = False) -> BotServiceRecord | None:
        statement = select(BotServiceRow).where(BotServiceRow.id == service_id)
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _service(row) if row is not None else None

    def list_active(self, *, after_id: UUID | None, limit: int) -> list[BotServiceRecord]:
        statement = select(BotServiceRow).where(BotServiceRow.deleted_at.is_(None))
        if after_id is not None:
            statement = statement.where(BotServiceRow.id > after_id)
        rows = self._session.scalars(
            statement.order_by(BotServiceRow.id).limit(_limit(limit))
        ).all()
        return [_service(row) for row in rows]

    def set_desired(
        self, service_id: UUID, expected_version: int, config_version_id: UUID, now: datetime
    ) -> BotServiceRecord | None:
        row = self._session.scalar(
            update(BotServiceRow)
            .where(
                BotServiceRow.id == service_id,
                BotServiceRow.row_version == expected_version,
                BotServiceRow.enabled.is_(True),
                BotServiceRow.deleted_at.is_(None),
            )
            .values(
                desired_config_version_id=config_version_id,
                row_version=BotServiceRow.row_version + 1,
                updated_at=now,
            )
            .returning(BotServiceRow)
        )
        return _service(row) if row is not None else None

    def set_applied_if_desired(
        self, service_id: UUID, config_version_id: UUID, now: datetime
    ) -> BotServiceRecord | None:
        row = self._session.scalar(
            update(BotServiceRow)
            .where(
                BotServiceRow.id == service_id,
                BotServiceRow.desired_config_version_id == config_version_id,
            )
            .values(
                applied_config_version_id=config_version_id,
                row_version=BotServiceRow.row_version + 1,
                updated_at=now,
            )
            .returning(BotServiceRow)
        )
        return _service(row) if row is not None else None

    def disable(
        self, service_id: UUID, expected_version: int, now: datetime
    ) -> BotServiceRecord | None:
        row = self._session.scalar(
            update(BotServiceRow)
            .where(
                BotServiceRow.id == service_id,
                BotServiceRow.row_version == expected_version,
                BotServiceRow.deleted_at.is_(None),
            )
            .values(enabled=False, row_version=BotServiceRow.row_version + 1, updated_at=now)
            .returning(BotServiceRow)
        )
        return _service(row) if row is not None else None

    def bump_version(
        self, service_id: UUID, expected_version: int, now: datetime
    ) -> BotServiceRecord | None:
        row = self._session.scalar(
            update(BotServiceRow)
            .where(
                BotServiceRow.id == service_id,
                BotServiceRow.row_version == expected_version,
                BotServiceRow.enabled.is_(True),
                BotServiceRow.deleted_at.is_(None),
            )
            .values(row_version=BotServiceRow.row_version + 1, updated_at=now)
            .returning(BotServiceRow)
        )
        return _service(row) if row is not None else None


class PostgresBotRegistrationGrantRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: BotRegistrationGrantRecord) -> None:
        self._session.add(
            BotRegistrationGrantRow(
                id=record.grant_id,
                service_id=record.service_id,
                secret_digest=record.secret_digest,
                expires_at=record.expires_at,
                consumed_at=record.consumed_at,
                consumed_by_identity_id=record.consumed_by_identity_id,
                row_version=record.row_version,
            )
        )

    def get(
        self, grant_id: UUID, *, for_update: bool = False
    ) -> BotRegistrationGrantRecord | None:
        statement = select(BotRegistrationGrantRow).where(BotRegistrationGrantRow.id == grant_id)
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _grant(row) if row is not None else None

    def consume(
        self, grant_id: UUID, expected_version: int, identity_id: UUID, now: datetime
    ) -> bool:
        result = self._session.execute(
            update(BotRegistrationGrantRow)
            .where(
                BotRegistrationGrantRow.id == grant_id,
                BotRegistrationGrantRow.row_version == expected_version,
                BotRegistrationGrantRow.consumed_at.is_(None),
            )
            .values(
                consumed_at=now,
                consumed_by_identity_id=identity_id,
                row_version=BotRegistrationGrantRow.row_version + 1,
            )
            .returning(BotRegistrationGrantRow.id)
        ).scalar_one_or_none()
        return result is not None


class PostgresBotWorkerIdentityRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: BotWorkerIdentityRecord) -> None:
        self._session.add(
            BotWorkerIdentityRow(
                id=record.identity_id,
                service_id=record.service_id,
                credential_digest=record.credential_digest,
                credential_version=record.credential_version,
                enabled=record.enabled,
                created_at=record.created_at,
            )
        )

    def get(
        self, identity_id: UUID, *, for_update: bool = False
    ) -> BotWorkerIdentityRecord | None:
        return self._get(BotWorkerIdentityRow.id == identity_id, for_update=for_update)

    def get_for_service(
        self, service_id: UUID, *, for_update: bool = False
    ) -> BotWorkerIdentityRecord | None:
        return self._get(BotWorkerIdentityRow.service_id == service_id, for_update=for_update)

    def _get(self, predicate: Any, *, for_update: bool) -> BotWorkerIdentityRecord | None:
        statement = select(BotWorkerIdentityRow).where(predicate)
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _identity(row) if row is not None else None

    def replace_credential(
        self, identity_id: UUID, credential_digest: str, credential_version: int, now: datetime
    ) -> bool:
        result = self._session.execute(
            update(BotWorkerIdentityRow)
            .where(BotWorkerIdentityRow.id == identity_id)
            .values(
                credential_digest=credential_digest,
                credential_version=credential_version,
                enabled=True,
                rotated_at=now,
            )
            .returning(BotWorkerIdentityRow.id)
        ).scalar_one_or_none()
        return result is not None

    def touch(self, identity_id: UUID, now: datetime) -> None:
        self._session.execute(
            update(BotWorkerIdentityRow)
            .where(BotWorkerIdentityRow.id == identity_id)
            .values(last_seen_at=now)
        )

    def disable_for_service(self, service_id: UUID, now: datetime) -> int:
        rows = self._session.scalars(
            update(BotWorkerIdentityRow)
            .where(
                BotWorkerIdentityRow.service_id == service_id,
                BotWorkerIdentityRow.enabled.is_(True),
            )
            .values(enabled=False, rotated_at=now)
            .returning(BotWorkerIdentityRow.id)
        ).all()
        return len(rows)


class PostgresBotConfigRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def next_version_no(self, service_id: UUID) -> int:
        current = self._session.scalar(
            select(func.max(BotConfigVersionRow.version_no)).where(
                BotConfigVersionRow.service_id == service_id
            )
        )
        return int(current or 0) + 1

    def add_version(self, record: BotConfigVersionRecord) -> None:
        self._session.add(
            BotConfigVersionRow(
                id=record.config_version_id,
                service_id=record.service_id,
                version_no=record.version_no,
                settings=record.settings,
                secret_id=record.secret_id,
                onebot_service_id=record.onebot_service_id,
                config_hash=record.config_hash,
                created_at=record.created_at,
            )
        )

    def get_version(self, config_version_id: UUID) -> BotConfigVersionRecord | None:
        row = self._session.get(BotConfigVersionRow, config_version_id)
        return _config(row) if row is not None else None

    def add_application(self, record: BotConfigApplicationRecord) -> None:
        self._session.add(
            BotConfigApplicationRow(
                id=record.application_id,
                service_id=record.service_id,
                config_version_id=record.config_version_id,
                status=record.status.value,
                requested_at=record.requested_at,
                row_version=record.row_version,
            )
        )

    def get_application(
        self, application_id: UUID, *, for_update: bool = False
    ) -> BotConfigApplicationRecord | None:
        statement = select(BotConfigApplicationRow).where(
            BotConfigApplicationRow.id == application_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _application(row) if row is not None else None

    def get_pending_for_service(self, service_id: UUID) -> BotConfigApplicationRecord | None:
        row = self._session.scalar(
            select(BotConfigApplicationRow)
            .join(
                BotServiceRow,
                BotServiceRow.desired_config_version_id
                == BotConfigApplicationRow.config_version_id,
            )
            .where(
                BotConfigApplicationRow.service_id == service_id,
                BotConfigApplicationRow.status == BotConfigApplicationStatus.PENDING.value,
                BotServiceRow.enabled.is_(True),
                BotServiceRow.deleted_at.is_(None),
            )
        )
        return _application(row) if row is not None else None

    def list_applications(
        self,
        service_id: UUID,
        *,
        before_requested_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[BotConfigApplicationRecord]:
        statement = select(BotConfigApplicationRow).where(
            BotConfigApplicationRow.service_id == service_id
        )
        if before_requested_at is not None and before_id is not None:
            statement = statement.where(
                tuple_(BotConfigApplicationRow.requested_at, BotConfigApplicationRow.id)
                < (before_requested_at, before_id)
            )
        rows = self._session.scalars(
            statement.order_by(
                BotConfigApplicationRow.requested_at.desc(), BotConfigApplicationRow.id.desc()
            ).limit(_limit(limit))
        ).all()
        return [_application(row) for row in rows]

    def get_application_by_receipt(
        self, receipt_event_id: UUID
    ) -> BotConfigApplicationRecord | None:
        row = self._session.scalar(
            select(BotConfigApplicationRow).where(
                BotConfigApplicationRow.receipt_event_id == receipt_event_id
            )
        )
        return _application(row) if row is not None else None

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
    ) -> BotConfigApplicationRecord | None:
        row = self._session.scalar(
            update(BotConfigApplicationRow)
            .where(
                BotConfigApplicationRow.id == application_id,
                BotConfigApplicationRow.row_version == expected_version,
                BotConfigApplicationRow.status == BotConfigApplicationStatus.PENDING.value,
            )
            .values(
                status=status.value,
                worker_instance_id=worker_instance_id,
                error_code=error_code,
                error_summary=error_summary,
                receipt_event_id=receipt_event_id,
                completed_at=now,
                row_version=BotConfigApplicationRow.row_version + 1,
            )
            .returning(BotConfigApplicationRow)
        )
        return _application(row) if row is not None else None

    def delete_settled_applications_before(self, cutoff: datetime, *, limit: int) -> int:
        candidates = (
            select(BotConfigApplicationRow.id)
            .where(
                BotConfigApplicationRow.status != BotConfigApplicationStatus.PENDING.value,
                BotConfigApplicationRow.completed_at < cutoff,
            )
            .order_by(BotConfigApplicationRow.completed_at, BotConfigApplicationRow.id)
            .limit(_limit(limit))
        )
        deleted = self._session.scalars(
            delete(BotConfigApplicationRow)
            .where(BotConfigApplicationRow.id.in_(candidates))
            .returning(BotConfigApplicationRow.id)
        ).all()
        return len(deleted)

    def delete_unreferenced_versions_before(
        self, cutoff: datetime, *, limit: int
    ) -> list[UUID | None]:
        candidates = (
            select(BotConfigVersionRow.id)
            .where(
                BotConfigVersionRow.created_at < cutoff,
                ~exists().where(
                    BotConfigApplicationRow.config_version_id == BotConfigVersionRow.id
                ),
                ~exists().where(BotHealthReportRow.config_version_id == BotConfigVersionRow.id),
                ~exists().where(
                    or_(
                        BotServiceRow.desired_config_version_id == BotConfigVersionRow.id,
                        BotServiceRow.applied_config_version_id == BotConfigVersionRow.id,
                    )
                ),
            )
            .order_by(BotConfigVersionRow.created_at, BotConfigVersionRow.id)
            .limit(_limit(limit))
        )
        return list(
            self._session.scalars(
                delete(BotConfigVersionRow)
                .where(BotConfigVersionRow.id.in_(candidates))
                .returning(BotConfigVersionRow.secret_id)
            ).all()
        )


class PostgresBotHealthRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

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
    ) -> bool:
        result = self._session.execute(
            postgres_insert(BotHealthReportRow)
            .values(
                id=report_id,
                receipt_event_id=receipt_event_id,
                service_id=service_id,
                identity_id=identity_id,
                config_version_id=config_version_id,
                status=status.value,
                diagnostics=diagnostics,
                reported_at=reported_at,
            )
            .on_conflict_do_nothing(index_elements=[BotHealthReportRow.receipt_event_id])
            .returning(BotHealthReportRow.id)
        ).scalar_one_or_none()
        return result is not None

    def get_by_receipt(self, receipt_event_id: UUID) -> BotHealthReportRecord | None:
        row = self._session.scalar(
            select(BotHealthReportRow).where(
                BotHealthReportRow.receipt_event_id == receipt_event_id
            )
        )
        return _health(row) if row is not None else None

    def list_for_service(
        self,
        service_id: UUID,
        *,
        before_reported_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[BotHealthReportRecord]:
        statement = select(BotHealthReportRow).where(
            BotHealthReportRow.service_id == service_id
        )
        if before_reported_at is not None and before_id is not None:
            statement = statement.where(
                tuple_(BotHealthReportRow.reported_at, BotHealthReportRow.id)
                < (before_reported_at, before_id)
            )
        rows = self._session.scalars(
            statement.order_by(
                BotHealthReportRow.reported_at.desc(), BotHealthReportRow.id.desc()
            ).limit(_limit(limit))
        ).all()
        return [_health(row) for row in rows]

    def delete_before(self, cutoff: datetime, *, limit: int) -> int:
        newer = aliased(BotHealthReportRow)
        candidates = (
            select(BotHealthReportRow.id)
            .where(
                BotHealthReportRow.reported_at < cutoff,
                exists().where(
                    newer.service_id == BotHealthReportRow.service_id,
                    tuple_(newer.reported_at, newer.id)
                    > tuple_(BotHealthReportRow.reported_at, BotHealthReportRow.id),
                ),
            )
            .order_by(BotHealthReportRow.reported_at, BotHealthReportRow.id)
            .limit(_limit(limit))
        )
        deleted = self._session.scalars(
            delete(BotHealthReportRow)
            .where(BotHealthReportRow.id.in_(candidates))
            .returning(BotHealthReportRow.id)
        ).all()
        return len(deleted)


def _limit(limit: int) -> int:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    return limit


def _service(row: BotServiceRow) -> BotServiceRecord:
    return BotServiceRecord(
        service_id=row.id,
        kind=BotServiceKind(row.kind),
        name=row.name,
        enabled=row.enabled,
        desired_config_version_id=row.desired_config_version_id,
        applied_config_version_id=row.applied_config_version_id,
        row_version=row.row_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _grant(row: BotRegistrationGrantRow) -> BotRegistrationGrantRecord:
    return BotRegistrationGrantRecord(
        grant_id=row.id,
        service_id=row.service_id,
        secret_digest=row.secret_digest,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
        consumed_by_identity_id=row.consumed_by_identity_id,
        row_version=row.row_version,
    )


def _identity(row: BotWorkerIdentityRow) -> BotWorkerIdentityRecord:
    return BotWorkerIdentityRecord(
        identity_id=row.id,
        service_id=row.service_id,
        credential_digest=row.credential_digest,
        credential_version=row.credential_version,
        enabled=row.enabled,
        created_at=row.created_at,
        rotated_at=row.rotated_at,
        last_seen_at=row.last_seen_at,
    )


def _config(row: BotConfigVersionRow) -> BotConfigVersionRecord:
    return BotConfigVersionRecord(
        config_version_id=row.id,
        service_id=row.service_id,
        version_no=row.version_no,
        settings=dict(row.settings),
        secret_id=row.secret_id,
        onebot_service_id=row.onebot_service_id,
        config_hash=row.config_hash,
        created_at=row.created_at,
    )


def _application(row: BotConfigApplicationRow) -> BotConfigApplicationRecord:
    return BotConfigApplicationRecord(
        application_id=row.id,
        service_id=row.service_id,
        config_version_id=row.config_version_id,
        status=BotConfigApplicationStatus(row.status),
        worker_instance_id=row.worker_instance_id,
        error_code=row.error_code,
        error_summary=row.error_summary,
        receipt_event_id=row.receipt_event_id,
        requested_at=row.requested_at,
        completed_at=row.completed_at,
        row_version=row.row_version,
    )


def _health(row: BotHealthReportRow) -> BotHealthReportRecord:
    return BotHealthReportRecord(
        report_id=row.id,
        receipt_event_id=row.receipt_event_id,
        service_id=row.service_id,
        identity_id=row.identity_id,
        config_version_id=row.config_version_id,
        status=BotHealthStatus(row.status),
        diagnostics=dict(row.diagnostics),
        reported_at=row.reported_at,
    )
