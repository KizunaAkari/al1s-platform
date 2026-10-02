"""Bounded claim/reservation transaction, including crashed-attempt accounting."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Table, and_, bindparam, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from al1s.adapters.postgres.notification_models import (
    EncryptedSecretRow,
    NotificationAttemptRow,
    NotificationChannelRow,
    NotificationDeliveryRow,
    NotificationIntentRow,
)
from al1s.notifications.types import ChannelKind, ClaimedNotificationDelivery, NotificationKind


def claim_deliveries(
    session: Session,
    *,
    worker_id: str,
    now: datetime,
    lease_duration: timedelta,
    limit: int,
    max_attempts: int,
) -> list[ClaimedNotificationDelivery]:
    if not 1 <= limit <= 50 or max_attempts < 1 or lease_duration.total_seconds() <= 0:
        raise ValueError("Invalid notification claim bounds")
    rows = session.execute(_claim_statement(now, limit)).all()
    changes: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    claimed = []
    for row in rows:
        delivery = row.NotificationDeliveryRow
        expired = delivery.status == "processing"
        if expired and delivery.attempt_count > 0:
            unknown.append(
                {
                    "id": uuid4(),
                    "delivery_id": delivery.id,
                    "attempt_no": delivery.attempt_count,
                    "outcome": "failed",
                    "retryable": True,
                    "error_type": "DeliveryOutcomeUnknown",
                    "error_code": "delivery_outcome_unknown",
                    "provider_message_id": None,
                    "started_at": delivery.available_at,
                    "completed_at": now,
                }
            )
        cap = min(3, max_attempts) if delivery.channel_kind == "smtp" else max_attempts
        status = (
            "cancelled"
            if not row.enabled or row.deleted_at is not None
            else "dead_letter"
            if delivery.attempt_count >= cap
            else "processing"
        )
        reserved = delivery.attempt_count + (status == "processing")
        changes.append(
            {
                "delivery_pk": delivery.id,
                "status": status,
                "attempt_count": reserved,
                "available_at": now,
                "row_version": delivery.row_version + 1,
                "locked_by": worker_id if status == "processing" else None,
                "locked_until": now + lease_duration if status == "processing" else None,
                "last_error_type": "DeliveryOutcomeUnknown"
                if expired
                else delivery.last_error_type,
                "last_error_code": "delivery_outcome_unknown"
                if expired
                else delivery.last_error_code,
                "cancelled_at": now if status == "cancelled" else delivery.cancelled_at,
                "dead_lettered_at": now if status == "dead_letter" else delivery.dead_lettered_at,
            }
        )
        if status == "processing":
            claimed.append(_claimed(row, reserved, now))
    if unknown:
        session.execute(
            insert(NotificationAttemptRow)
            .values(unknown)
            .on_conflict_do_nothing(
                index_elements=[
                    NotificationAttemptRow.delivery_id,
                    NotificationAttemptRow.attempt_no,
                ]
            )
        )
    if changes:
        table = cast(Table, NotificationDeliveryRow.__table__)
        fields: dict[str, Any] = {
            name: bindparam(name) for name in changes[0] if name != "delivery_pk"
        }
        session.execute(
            update(table).where(table.c.id == bindparam("delivery_pk")).values(fields), changes
        )
    return claimed


def _claim_statement(now: datetime, limit: int) -> Any:
    return (
        select(
            NotificationDeliveryRow,
            NotificationIntentRow.notification_kind,
            NotificationIntentRow.payload,
            NotificationChannelRow.settings,
            NotificationChannelRow.bot_service_id,
            NotificationChannelRow.enabled,
            NotificationChannelRow.deleted_at,
            EncryptedSecretRow.ciphertext,
            EncryptedSecretRow.key_id,
        )
        .join(NotificationIntentRow, NotificationIntentRow.id == NotificationDeliveryRow.intent_id)
        .join(
            NotificationChannelRow, NotificationChannelRow.id == NotificationDeliveryRow.channel_id
        )
        .outerjoin(EncryptedSecretRow, EncryptedSecretRow.id == NotificationChannelRow.secret_id)
        .where(
            or_(
                and_(
                    NotificationDeliveryRow.status == "pending",
                    NotificationDeliveryRow.available_at <= now,
                ),
                and_(
                    NotificationDeliveryRow.status == "processing",
                    NotificationDeliveryRow.locked_until <= now,
                ),
            )
        )
        .order_by(
            NotificationDeliveryRow.available_at,
            NotificationDeliveryRow.created_at,
            NotificationDeliveryRow.id,
        )
        .limit(limit)
        .with_for_update(of=NotificationDeliveryRow, skip_locked=True)
    )


def _claimed(row: Any, reserved: int, now: datetime) -> ClaimedNotificationDelivery:
    item = row.NotificationDeliveryRow
    return ClaimedNotificationDelivery(
        delivery_id=item.id,
        intent_id=item.intent_id,
        channel_id=item.channel_id,
        channel_kind=ChannelKind(item.channel_kind),
        notification_kind=NotificationKind(row.notification_kind),
        targets=tuple(item.targets),
        template_key=item.template_key,
        payload=dict(row.payload),
        channel_settings=dict(row.settings),
        bot_service_id=row.bot_service_id,
        encrypted_secret=row.ciphertext,
        secret_key_id=row.key_id,
        attempt_count=reserved,
        row_version=item.row_version + 1,
        started_at=now,
    )
