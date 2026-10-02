from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, case, func, insert, or_, select, update, values
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import column

from al1s.adapters.postgres.notification_claims import claim_deliveries
from al1s.adapters.postgres.notification_models import (
    NotificationAttemptRow,
    NotificationChannelRow,
    NotificationDeliveryRow,
    NotificationIntentRow,
    NotificationRouteRow,
)
from al1s.notifications.errors import NotificationDomainError
from al1s.notifications.types import (
    AttemptOutcome,
    ChannelKind,
    ClaimedNotificationDelivery,
    DeliveryOutcome,
    DeliveryStatus,
    EnabledNotificationRoute,
    NewNotificationDelivery,
    NotificationAttemptRecord,
    NotificationChannelRecord,
    NotificationDeliveryView,
    NotificationIntentRecord,
    NotificationKind,
    NotificationRouteRecord,
)

MAX_NOTIFICATION_BATCH = 50


def _bounded_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_NOTIFICATION_BATCH:
        raise ValueError(f"limit must be between 1 and {MAX_NOTIFICATION_BATCH}")
    return limit


def _retry_delay(
    attempt_count: int,
    base_retry_delay: timedelta,
    max_retry_delay: timedelta,
) -> timedelta:
    seconds = min(
        base_retry_delay.total_seconds() * (2 ** max(0, attempt_count - 1)),
        max_retry_delay.total_seconds(),
    )
    return timedelta(seconds=seconds)


class PostgresNotificationChannelRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: NotificationChannelRecord) -> bool:
        statement = (
            postgres_insert(NotificationChannelRow)
            .values(
                id=record.channel_id,
                kind=record.kind.value,
                name=record.name,
                enabled=record.enabled,
                settings=dict(record.settings),
                secret_id=record.secret_id,
                bot_service_id=record.bot_service_id,
                row_version=record.row_version,
                created_at=record.created_at,
                updated_at=record.updated_at,
                deleted_at=record.deleted_at,
            )
            .on_conflict_do_nothing()
            .returning(NotificationChannelRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def get(
        self, channel_id: UUID, *, for_update: bool = False
    ) -> NotificationChannelRecord | None:
        statement = select(NotificationChannelRow).where(NotificationChannelRow.id == channel_id)
        if for_update:
            statement = statement.with_for_update()
        row = self._session.execute(statement).scalar_one_or_none()
        return _channel_record(row) if row else None

    def update(self, record: NotificationChannelRecord, *, expected_version: int) -> bool:
        try:
            updated_id = self._session.execute(
                update(NotificationChannelRow)
                .where(
                    NotificationChannelRow.id == record.channel_id,
                    NotificationChannelRow.row_version == expected_version,
                )
                .values(
                    name=record.name,
                    enabled=record.enabled,
                    settings=dict(record.settings),
                    secret_id=record.secret_id,
                    bot_service_id=record.bot_service_id,
                    row_version=record.row_version,
                    updated_at=record.updated_at,
                    deleted_at=record.deleted_at,
                )
                .returning(NotificationChannelRow.id)
            ).scalar_one_or_none()
        except IntegrityError as error:
            constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            if constraint != "uq_notification_channels_active_name":
                raise
            raise NotificationDomainError(
                "notification_channel_conflict", "Channel name already exists", 409
            ) from error
        return updated_id is not None

    def list_active(
        self,
        *,
        before_created_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[NotificationChannelRecord]:
        statement = select(NotificationChannelRow).where(
            NotificationChannelRow.deleted_at.is_(None)
        )
        statement = _apply_descending_cursor(
            statement,
            NotificationChannelRow.created_at,
            NotificationChannelRow.id,
            before_created_at,
            before_id,
        )
        rows = self._session.scalars(
            statement.order_by(
                NotificationChannelRow.created_at.desc(), NotificationChannelRow.id.desc()
            ).limit(_bounded_page(limit))
        ).all()
        return [_channel_record(row) for row in rows]


class PostgresNotificationRouteRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: NotificationRouteRecord) -> bool:
        statement = (
            postgres_insert(NotificationRouteRow)
            .values(
                id=record.route_id,
                notification_kind=record.notification_kind.value,
                channel_id=record.channel_id,
                targets=list(record.targets),
                template_key=record.template_key,
                enabled=record.enabled,
                row_version=record.row_version,
                created_at=record.created_at,
                updated_at=record.updated_at,
                deleted_at=record.deleted_at,
            )
            .on_conflict_do_nothing()
            .returning(NotificationRouteRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def get(self, route_id: UUID, *, for_update: bool = False) -> NotificationRouteRecord | None:
        statement = select(NotificationRouteRow).where(NotificationRouteRow.id == route_id)
        if for_update:
            statement = statement.with_for_update()
        row = self._session.execute(statement).scalar_one_or_none()
        return _route_record(row) if row else None

    def update(self, record: NotificationRouteRecord, *, expected_version: int) -> bool:
        updated_id = self._session.execute(
            update(NotificationRouteRow)
            .where(
                NotificationRouteRow.id == record.route_id,
                NotificationRouteRow.row_version == expected_version,
            )
            .values(
                targets=list(record.targets),
                template_key=record.template_key,
                enabled=record.enabled,
                row_version=record.row_version,
                updated_at=record.updated_at,
                deleted_at=record.deleted_at,
            )
            .returning(NotificationRouteRow.id)
        ).scalar_one_or_none()
        return updated_id is not None

    def soft_delete_for_channel(self, channel_id: UUID, *, now: datetime) -> int:
        result = self._session.execute(
            update(NotificationRouteRow)
            .where(
                NotificationRouteRow.channel_id == channel_id,
                NotificationRouteRow.deleted_at.is_(None),
            )
            .values(
                enabled=False,
                deleted_at=now,
                updated_at=now,
                row_version=NotificationRouteRow.row_version + 1,
            )
        )
        return cast(CursorResult[Any], result).rowcount

    def list_enabled_for_kind(self, kind: str) -> list[EnabledNotificationRoute]:
        rows = self._session.execute(
            select(NotificationRouteRow, NotificationChannelRow.kind.label("channel_kind"),
                   NotificationChannelRow.bot_service_id)
            .join(
                NotificationChannelRow,
                NotificationChannelRow.id == NotificationRouteRow.channel_id,
            )
            .where(
                NotificationRouteRow.notification_kind == kind,
                NotificationRouteRow.enabled.is_(True),
                NotificationRouteRow.deleted_at.is_(None),
                NotificationChannelRow.enabled.is_(True),
                NotificationChannelRow.deleted_at.is_(None),
            )
            .order_by(NotificationRouteRow.created_at, NotificationRouteRow.id)
        ).all()
        return [
            EnabledNotificationRoute(
                route=_route_record(row.NotificationRouteRow),
                channel_kind=ChannelKind(row.channel_kind),
                bot_service_id=row.bot_service_id,
            )
            for row in rows
        ]

    def list_active(
        self,
        *,
        notification_kind: str | None,
        before_created_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[NotificationRouteRecord]:
        statement = select(NotificationRouteRow).where(NotificationRouteRow.deleted_at.is_(None))
        if notification_kind is not None:
            statement = statement.where(NotificationRouteRow.notification_kind == notification_kind)
        statement = _apply_descending_cursor(
            statement,
            NotificationRouteRow.created_at,
            NotificationRouteRow.id,
            before_created_at,
            before_id,
        )
        rows = self._session.scalars(
            statement.order_by(
                NotificationRouteRow.created_at.desc(), NotificationRouteRow.id.desc()
            ).limit(_bounded_page(limit))
        ).all()
        return [_route_record(row) for row in rows]


class PostgresNotificationIntentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_many(self, records: Sequence[NotificationIntentRecord]) -> None:
        if not records:
            return
        self._session.execute(insert(NotificationIntentRow), [{
            "id": record.intent_id, "source_event_id": record.source_event_id,
            "notification_kind": record.notification_kind.value,
            "source_type": record.source_type, "source_id": record.source_id,
            "correlation_id": record.correlation_id, "schema_version": record.schema_version,
            "payload": dict(record.payload), "disposition": record.disposition,
            "occurred_at": record.occurred_at, "created_at": record.created_at,
        } for record in records])

    def add(self, record: NotificationIntentRecord) -> bool:
        statement = (
            postgres_insert(NotificationIntentRow)
            .values(
                id=record.intent_id,
                source_event_id=record.source_event_id,
                notification_kind=record.notification_kind.value,
                source_type=record.source_type,
                source_id=record.source_id,
                correlation_id=record.correlation_id,
                schema_version=record.schema_version,
                payload=dict(record.payload),
                disposition=record.disposition,
                occurred_at=record.occurred_at,
                created_at=record.created_at,
            )
            .on_conflict_do_nothing(index_elements=[NotificationIntentRow.source_event_id])
            .returning(NotificationIntentRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def get_by_source_event(self, source_event_id: UUID) -> NotificationIntentRecord | None:
        row = self._session.execute(
            select(NotificationIntentRow).where(
                NotificationIntentRow.source_event_id == source_event_id
            )
        ).scalar_one_or_none()
        return _intent_record(row) if row else None


class PostgresNotificationDeliveryRepository:
    def count_statuses(self, intent_id: UUID) -> dict[str, int]:
        rows = self._session.execute(
            select(NotificationDeliveryRow.status, func.count())
            .where(NotificationDeliveryRow.intent_id == intent_id)
            .group_by(NotificationDeliveryRow.status)
        ).all()
        return {status: count for status, count in rows}

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_many(self, records: Sequence[NewNotificationDelivery]) -> int:
        if not records:
            return 0
        records = [
            replace(
                record, delivery_id=record.delivery_id if index == 0 else uuid4(), targets=(target,)
            )
            for record in records
            for index, target in enumerate(dict.fromkeys(record.targets))
        ]
        statement = insert(NotificationDeliveryRow).values(
            [
                {
                    "id": record.delivery_id,
                    "intent_id": record.intent_id,
                    "route_id": record.route_id,
                    "channel_id": record.channel_id,
                    "channel_kind": record.channel_kind.value,
                    "targets": list(record.targets),
                    "template_key": record.template_key,
                    "status": DeliveryStatus.PENDING.value,
                    "attempt_count": 0,
                    "available_at": record.available_at,
                    "row_version": 1,
                    "created_at": record.available_at,
                }
                for record in records
            ]
        )
        self._session.execute(statement)
        return len(records)

    def cancel_pending_for_channel(self, channel_id: UUID, *, now: datetime) -> int:
        return self._cancel_pending(NotificationDeliveryRow.channel_id == channel_id, now=now)

    def cancel_pending_for_route(self, route_id: UUID, *, now: datetime) -> int:
        return self._cancel_pending(NotificationDeliveryRow.route_id == route_id, now=now)

    def _cancel_pending(self, predicate: Any, *, now: datetime) -> int:
        cancelled = self._session.execute(
            update(NotificationDeliveryRow)
            .where(
                predicate,
                NotificationDeliveryRow.status == DeliveryStatus.PENDING.value,
            )
            .values(
                status=DeliveryStatus.CANCELLED.value,
                cancelled_at=now,
                locked_by=None,
                locked_until=None,
                row_version=NotificationDeliveryRow.row_version + 1,
            )
            .returning(NotificationDeliveryRow.id)
        ).scalars()
        return len(cancelled.all())

    def renew_batch(
        self,
        deliveries: Sequence[ClaimedNotificationDelivery],
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> int:
        if not deliveries:
            return 0
        _bounded_limit(len(deliveries))
        if lease_duration.total_seconds() <= 0:
            raise ValueError("lease_duration must be positive")
        row = NotificationDeliveryRow
        statement = (
            update(row)
            .where(
                row.status == DeliveryStatus.PROCESSING.value,
                row.locked_by == worker_id,
                row.locked_until > now,
                or_(
                    *(
                        and_(row.id == item.delivery_id, row.row_version == item.row_version)
                        for item in deliveries
                    )
                ),
            )
            .values(locked_until=now + lease_duration)
            .returning(row.id)
        )
        return len(self._session.scalars(statement).all())

    def claim_batch(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
        max_attempts: int = 5,
    ) -> list[ClaimedNotificationDelivery]:
        return claim_deliveries(
            self._session,
            worker_id=worker_id,
            now=now,
            lease_duration=lease_duration,
            limit=_bounded_limit(limit),
            max_attempts=max_attempts,
        )

    def defer_evidence(
        self, outcomes: Sequence[DeliveryOutcome], *, worker_id: str, now: datetime
    ) -> int:
        if not outcomes:
            return 0
        if len(outcomes) > MAX_NOTIFICATION_BATCH or any(
            not o.evidence_pending or o.attempt_count < 1 for o in outcomes
        ):
            raise ValueError("Invalid evidence deferral")
        expected = values(
            column("id", NotificationDeliveryRow.id.type),
            column("version", NotificationDeliveryRow.row_version.type),
            column("count", NotificationDeliveryRow.attempt_count.type),
            name="waiting_evidence",
        ).data([(o.delivery_id, o.row_version, o.attempt_count) for o in outcomes])
        rows = self._session.execute(
            update(NotificationDeliveryRow)
            .where(
                NotificationDeliveryRow.id == expected.c.id,
                NotificationDeliveryRow.row_version == expected.c.version,
                NotificationDeliveryRow.attempt_count == expected.c.count,
                NotificationDeliveryRow.channel_kind == ChannelKind.QQ.value,
                NotificationDeliveryRow.status == DeliveryStatus.PROCESSING.value,
                NotificationDeliveryRow.locked_by == worker_id,
                NotificationDeliveryRow.locked_until > now,
            )
            .values(
                status=DeliveryStatus.PENDING.value,
                attempt_count=expected.c.count - 1,
                available_at=now + timedelta(minutes=1),
                locked_by=None,
                locked_until=None,
                last_error_code="evidence_pending",
                last_error_type=None,
                row_version=NotificationDeliveryRow.row_version + 1,
            )
            .returning(NotificationDeliveryRow.id)
        ).all()
        return len(rows)

    def settle_batch(
        self,
        outcomes: Sequence[DeliveryOutcome],
        *,
        worker_id: str,
        now: datetime,
        max_attempts: int,
        base_retry_delay: timedelta,
        max_retry_delay: timedelta,
    ) -> tuple[int, int, int]:
        if not outcomes:
            return 0, 0, 0
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        prepared: list[dict[str, Any]] = []
        by_id = {outcome.delivery_id: outcome for outcome in outcomes}
        for outcome in outcomes:
            next_attempt = outcome.attempt_count
            sent = outcome.outcome is AttemptOutcome.SENT
            dead = not sent and (not outcome.retryable or next_attempt >= max_attempts)
            status = (
                DeliveryStatus.SENT.value
                if sent
                else DeliveryStatus.DEAD_LETTER.value
                if dead
                else DeliveryStatus.PENDING.value
            )
            prepared.append(
                {
                    "delivery_id": outcome.delivery_id,
                    "expected_version": outcome.row_version,
                    "attempt_count": next_attempt,
                    "next_status": status,
                    "available_at": (
                        now + _retry_delay(next_attempt, base_retry_delay, max_retry_delay)
                        if status == DeliveryStatus.PENDING.value
                        else now
                    ),
                    "error_type": outcome.error_type,
                    "error_code": outcome.error_code,
                    "provider_message_id": outcome.provider_message_id,
                }
            )
        outcome_values = values(
            column("delivery_id", NotificationDeliveryRow.id.type),
            column("expected_version", NotificationDeliveryRow.row_version.type),
            column("attempt_count", NotificationDeliveryRow.attempt_count.type),
            column("next_status", NotificationDeliveryRow.status.type),
            column("available_at", NotificationDeliveryRow.available_at.type),
            column("error_type", NotificationDeliveryRow.last_error_type.type),
            column("error_code", NotificationDeliveryRow.last_error_code.type),
            column("provider_message_id", NotificationDeliveryRow.provider_message_id.type),
            name="notification_outcomes",
        ).data(
            [
                (
                    item["delivery_id"],
                    item["expected_version"],
                    item["attempt_count"],
                    item["next_status"],
                    item["available_at"],
                    item["error_type"],
                    item["error_code"],
                    item["provider_message_id"],
                )
                for item in prepared
            ]
        )
        final_status = case(
            (
                and_(
                    NotificationDeliveryRow.channel_kind == ChannelKind.SMTP.value,
                    outcome_values.c.attempt_count >= 3,
                    outcome_values.c.next_status == DeliveryStatus.PENDING.value,
                ),
                DeliveryStatus.DEAD_LETTER.value,
            ),
            else_=outcome_values.c.next_status,
        )
        updated = self._session.execute(
            update(NotificationDeliveryRow)
            .where(
                NotificationDeliveryRow.id == outcome_values.c.delivery_id,
                NotificationDeliveryRow.row_version == outcome_values.c.expected_version,
                NotificationDeliveryRow.status == DeliveryStatus.PROCESSING.value,
                NotificationDeliveryRow.locked_by == worker_id,
                NotificationDeliveryRow.locked_until > now,
                NotificationDeliveryRow.attempt_count == outcome_values.c.attempt_count,
            )
            .values(
                status=final_status,
                attempt_count=outcome_values.c.attempt_count,
                available_at=outcome_values.c.available_at,
                locked_by=None,
                locked_until=None,
                last_error_type=outcome_values.c.error_type,
                last_error_code=outcome_values.c.error_code,
                provider_message_id=outcome_values.c.provider_message_id,
                sent_at=case(
                    (outcome_values.c.next_status == DeliveryStatus.SENT.value, now),
                    else_=NotificationDeliveryRow.sent_at,
                ),
                dead_lettered_at=case(
                    (
                        final_status == DeliveryStatus.DEAD_LETTER.value,
                        now,
                    ),
                    else_=NotificationDeliveryRow.dead_lettered_at,
                ),
                row_version=NotificationDeliveryRow.row_version + 1,
            )
            .returning(NotificationDeliveryRow.id, NotificationDeliveryRow.status)
        ).all()
        updated_status = {row.id: row.status for row in updated}
        if updated_status:
            self._session.execute(
                insert(NotificationAttemptRow).values(
                    [
                        {
                            "id": uuid4(),
                            "delivery_id": delivery_id,
                            "attempt_no": by_id[delivery_id].attempt_count,
                            "outcome": by_id[delivery_id].outcome.value,
                            "retryable": by_id[delivery_id].retryable,
                            "error_type": by_id[delivery_id].error_type,
                            "error_code": by_id[delivery_id].error_code,
                            "provider_message_id": by_id[delivery_id].provider_message_id,
                            "started_at": by_id[delivery_id].started_at or now,
                            "completed_at": now,
                        }
                        for delivery_id in updated_status
                    ]
                )
            )
        sent_count = sum(status == DeliveryStatus.SENT.value for status in updated_status.values())
        dead_count = sum(
            status == DeliveryStatus.DEAD_LETTER.value for status in updated_status.values()
        )
        failed_count = len(updated_status) - sent_count - dead_count
        return sent_count, failed_count, dead_count

    def get_for_intent(self, intent_id: UUID) -> NotificationDeliveryView | None:
        row = self._session.execute(
            _delivery_view_statement()
            .where(NotificationDeliveryRow.intent_id == intent_id)
            .order_by(NotificationDeliveryRow.created_at, NotificationDeliveryRow.id)
            .limit(1)
        ).one_or_none()
        return _delivery_view(row) if row else None

    def get(self, delivery_id: UUID) -> NotificationDeliveryView | None:
        row = self._session.execute(
            _delivery_view_statement().where(NotificationDeliveryRow.id == delivery_id)
        ).one_or_none()
        return _delivery_view(row) if row else None

    def list_page(
        self,
        *,
        status: str | None,
        before_created_at: datetime | None,
        before_id: UUID | None,
        limit: int,
        notification_kind: str | None = None,
    ) -> list[NotificationDeliveryView]:
        statement = _delivery_view_statement()
        if status is not None:
            statement = statement.where(NotificationDeliveryRow.status == status)
        if notification_kind is not None:
            statement = statement.where(
                NotificationIntentRow.notification_kind == notification_kind
            )
        statement = _apply_descending_cursor(
            statement,
            NotificationDeliveryRow.created_at,
            NotificationDeliveryRow.id,
            before_created_at,
            before_id,
        )
        rows = self._session.execute(
            statement.order_by(
                NotificationDeliveryRow.created_at.desc(), NotificationDeliveryRow.id.desc()
            ).limit(_bounded_page(limit))
        ).all()
        return [_delivery_view(row) for row in rows]

    def list_attempts(self, delivery_id: UUID, *, limit: int) -> list[NotificationAttemptRecord]:
        rows = self._session.scalars(
            select(NotificationAttemptRow)
            .where(NotificationAttemptRow.delivery_id == delivery_id)
            .order_by(NotificationAttemptRow.attempt_no)
            .limit(_bounded_page(limit))
        ).all()
        return [_attempt_record(row) for row in rows]


def _channel_record(row: NotificationChannelRow) -> NotificationChannelRecord:
    return NotificationChannelRecord(
        channel_id=row.id,
        kind=ChannelKind(row.kind),
        name=row.name,
        enabled=row.enabled,
        settings=dict(row.settings),
        secret_id=row.secret_id,
        bot_service_id=row.bot_service_id,
        row_version=row.row_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _route_record(row: NotificationRouteRow) -> NotificationRouteRecord:
    return NotificationRouteRecord(
        route_id=row.id,
        notification_kind=NotificationKind(row.notification_kind),
        channel_id=row.channel_id,
        targets=tuple(row.targets),
        template_key=row.template_key,
        enabled=row.enabled,
        row_version=row.row_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _intent_record(row: NotificationIntentRow) -> NotificationIntentRecord:
    return NotificationIntentRecord(
        intent_id=row.id,
        source_event_id=row.source_event_id,
        notification_kind=NotificationKind(row.notification_kind),
        source_type=row.source_type,
        source_id=row.source_id,
        correlation_id=row.correlation_id,
        schema_version=row.schema_version,
        payload=dict(row.payload),
        disposition=row.disposition,
        occurred_at=row.occurred_at,
        created_at=row.created_at,
    )


def _bounded_page(limit: int) -> int:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    return limit


def _apply_descending_cursor(
    statement: Any,
    created_column: Any,
    id_column: Any,
    before_created_at: datetime | None,
    before_id: UUID | None,
) -> Any:
    if (before_created_at is None) != (before_id is None):
        raise ValueError("cursor timestamp and identifier must be supplied together")
    if before_created_at is None or before_id is None:
        return statement
    return statement.where(
        or_(
            created_column < before_created_at,
            and_(created_column == before_created_at, id_column < before_id),
        )
    )


def _delivery_view_statement() -> Any:
    return (
        select(
            NotificationDeliveryRow,
            NotificationIntentRow.notification_kind,
            NotificationIntentRow.source_type,
            NotificationIntentRow.source_id,
            NotificationChannelRow.name.label("channel_name"),
        )
        .join(NotificationIntentRow, NotificationIntentRow.id == NotificationDeliveryRow.intent_id)
        .join(
            NotificationChannelRow, NotificationChannelRow.id == NotificationDeliveryRow.channel_id
        )
    )


def _delivery_view(row: Any) -> NotificationDeliveryView:
    delivery = row.NotificationDeliveryRow
    return NotificationDeliveryView(
        delivery_id=delivery.id,
        intent_id=delivery.intent_id,
        notification_kind=NotificationKind(row.notification_kind),
        source_type=row.source_type,
        source_id=row.source_id,
        channel_id=delivery.channel_id,
        channel_kind=ChannelKind(delivery.channel_kind),
        channel_name=row.channel_name,
        targets=tuple(delivery.targets),
        template_key=delivery.template_key,
        status=DeliveryStatus(delivery.status),
        attempt_count=delivery.attempt_count,
        available_at=delivery.available_at,
        last_error_type=delivery.last_error_type,
        last_error_code=delivery.last_error_code,
        created_at=delivery.created_at,
        sent_at=delivery.sent_at,
        dead_lettered_at=delivery.dead_lettered_at,
        cancelled_at=delivery.cancelled_at,
    )


def _attempt_record(row: NotificationAttemptRow) -> NotificationAttemptRecord:
    return NotificationAttemptRecord(
        attempt_id=row.id,
        delivery_id=row.delivery_id,
        attempt_no=row.attempt_no,
        outcome=AttemptOutcome(row.outcome),
        retryable=row.retryable,
        error_type=row.error_type,
        error_code=row.error_code,
        provider_message_id=row.provider_message_id,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )
