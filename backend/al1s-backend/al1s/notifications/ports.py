from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any, Protocol
from uuid import UUID

from al1s.bots.ports import BotServiceRepository
from al1s.kernel.ports import AuditRepository, OutboxRepository
from al1s.notifications.review_ports import ReviewRepository
from al1s.notifications.types import (
    AdapterReceipt,
    ClaimedNotificationDelivery,
    DeliveryOutcome,
    EnabledNotificationRoute,
    NewNotificationDelivery,
    NotificationAttemptRecord,
    NotificationChannelRecord,
    NotificationDeliveryView,
    NotificationIntentRecord,
    NotificationMessage,
    NotificationRouteRecord,
)
from al1s.secrets.ports import SecretRepository


class NotificationSecretRepository(SecretRepository, Protocol):
    pass


class NotificationChannelRepository(Protocol):
    def add(self, record: NotificationChannelRecord) -> bool: ...

    def get(
        self, channel_id: UUID, *, for_update: bool = False
    ) -> NotificationChannelRecord | None: ...

    def update(self, record: NotificationChannelRecord, *, expected_version: int) -> bool: ...

    def list_active(
        self,
        *,
        before_created_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[NotificationChannelRecord]: ...


class NotificationRouteRepository(Protocol):
    def add(self, record: NotificationRouteRecord) -> bool: ...

    def get(
        self, route_id: UUID, *, for_update: bool = False
    ) -> NotificationRouteRecord | None: ...

    def update(self, record: NotificationRouteRecord, *, expected_version: int) -> bool: ...

    def soft_delete_for_channel(self, channel_id: UUID, *, now: datetime) -> int: ...

    def list_enabled_for_kind(self, kind: str) -> list[EnabledNotificationRoute]: ...

    def list_active(
        self,
        *,
        notification_kind: str | None,
        before_created_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[NotificationRouteRecord]: ...


class NotificationIntentRepository(Protocol):
    def add(self, record: NotificationIntentRecord) -> bool: ...
    def add_many(self, records: Sequence[NotificationIntentRecord]) -> None: ...

    def get_by_source_event(self, source_event_id: UUID) -> NotificationIntentRecord | None: ...


class NotificationDeliveryRepository(Protocol):
    def defer_evidence(
        self, outcomes: Sequence[DeliveryOutcome], *, worker_id: str, now: datetime
    ) -> int: ...
    def add_many(self, records: Sequence[NewNotificationDelivery]) -> int: ...

    def cancel_pending_for_channel(self, channel_id: UUID, *, now: datetime) -> int: ...

    def cancel_pending_for_route(self, route_id: UUID, *, now: datetime) -> int: ...

    def claim_batch(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
        max_attempts: int = 5,
    ) -> list[ClaimedNotificationDelivery]: ...

    def renew_batch(
        self,
        deliveries: Sequence[ClaimedNotificationDelivery],
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> int: ...

    def settle_batch(
        self,
        outcomes: Sequence[DeliveryOutcome],
        *,
        worker_id: str,
        now: datetime,
        max_attempts: int,
        base_retry_delay: timedelta,
        max_retry_delay: timedelta,
    ) -> tuple[int, int, int]: ...

    def get_for_intent(self, intent_id: UUID) -> NotificationDeliveryView | None: ...

    def count_statuses(self, intent_id: UUID) -> dict[str, int]: ...

    def get(self, delivery_id: UUID) -> NotificationDeliveryView | None: ...

    def list_page(
        self,
        *,
        status: str | None,
        before_created_at: datetime | None,
        before_id: UUID | None,
        limit: int,
        notification_kind: str | None = None,
    ) -> list[NotificationDeliveryView]: ...

    def list_attempts(
        self, delivery_id: UUID, *, limit: int
    ) -> list[NotificationAttemptRecord]: ...


class NotificationChannelAdapter(Protocol):
    def send(self, message: NotificationMessage, *, idempotency_key: str) -> AdapterReceipt: ...


class NotificationUnitOfWork(Protocol):
    reviews: ReviewRepository
    forward_rules: DiscordForwardRuleRepository
    bot_services: BotServiceRepository
    secrets: NotificationSecretRepository
    channels: NotificationChannelRepository
    routes: NotificationRouteRepository
    intents: NotificationIntentRepository
    deliveries: NotificationDeliveryRepository
    audit: AuditRepository
    outbox: OutboxRepository

    def __enter__(self) -> NotificationUnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


JsonObject = dict[str, Any]


class DiscordForwardRuleRepository(Protocol):
    def enabled_actions(
        self,
        service_id: UUID,
        rule_id: UUID,
        version: int,
        guild_id: str,
        channel_id: str,
    ) -> list[DiscordForwardAction] | None: ...


class DiscordForwardAction(Protocol):
    id: UUID
    channel_id: UUID
    kind: str
    target: str
    review_policy: str
