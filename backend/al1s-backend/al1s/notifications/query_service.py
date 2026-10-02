from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from al1s.notifications.errors import NotificationDomainError
from al1s.notifications.ports import NotificationUnitOfWork
from al1s.notifications.types import (
    DeliveryStatus,
    NotificationAttemptRecord,
    NotificationChannelRecord,
    NotificationDeliveryView,
    NotificationKind,
    NotificationRouteRecord,
    NotificationSummary,
)


class NotificationQueryService:
    def __init__(self, uow_factory: Callable[[], NotificationUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def source_summary(self, source_event_id: UUID) -> NotificationSummary:
        with self._uow_factory() as uow:
            intent = uow.intents.get_by_source_event(source_event_id)
            if intent is None:
                raise NotificationDomainError(
                    "notification_intent_not_found", "Source event not received", 404,
                )
            counts = uow.deliveries.count_statuses(intent.intent_id)
        if intent.disposition == "no_route":
            status = "no_route"
        elif counts.get("pending", 0) or counts.get("processing", 0):
            status = "processing"
        elif not counts:
            status = "inconsistent"
        elif counts.get("sent", 0) == sum(counts.values()):
            status = "sent"
        elif counts.get("sent", 0):
            status = "partial_failure"
        elif counts.get("cancelled", 0) == sum(counts.values()):
            status = "cancelled"
        else:
            status = "failed"
        return NotificationSummary(intent.intent_id, source_event_id, status, counts)

    def list_channels(
        self,
        *,
        before_created_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[NotificationChannelRecord]:
        with self._uow_factory() as uow:
            return uow.channels.list_active(
                before_created_at=before_created_at,
                before_id=before_id,
                limit=limit,
            )

    def list_routes(
        self,
        *,
        notification_kind: NotificationKind | None,
        before_created_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[NotificationRouteRecord]:
        with self._uow_factory() as uow:
            return uow.routes.list_active(
                notification_kind=(notification_kind.value if notification_kind else None),
                before_created_at=before_created_at,
                before_id=before_id,
                limit=limit,
            )

    def list_deliveries(
        self,
        *,
        status: DeliveryStatus | None,
        before_created_at: datetime | None,
        before_id: UUID | None,
        limit: int,
        notification_kind: NotificationKind | None = None,
    ) -> list[NotificationDeliveryView]:
        with self._uow_factory() as uow:
            return uow.deliveries.list_page(
                status=status.value if status else None,
                notification_kind=notification_kind.value if notification_kind else None,
                before_created_at=before_created_at,
                before_id=before_id,
                limit=limit,
            )

    def get_delivery(
        self, delivery_id: UUID
    ) -> tuple[NotificationDeliveryView, list[NotificationAttemptRecord]]:
        with self._uow_factory() as uow:
            delivery = uow.deliveries.get(delivery_id)
            if delivery is None:
                raise NotificationDomainError(
                    "notification_delivery_not_found", "Delivery not found", 404
                )
            return delivery, uow.deliveries.list_attempts(delivery_id, limit=100)
