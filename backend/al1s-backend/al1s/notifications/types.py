from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class ChannelKind(StrEnum):
    SMTP = "smtp"
    QQ = "qq"
    DISCORD = "discord"


class SecretChange(StrEnum):
    PRESERVE = "preserve"
    SET = "set"
    CLEAR = "clear"


class NotificationKind(StrEnum):
    FORWARD = "forward"
    CONDITIONAL_SKIP = "conditional_skip"
    SCRIPT_FAILURE = "script_failure"
    STORAGE_LOW = "storage_low"
    TERMINAL_ALERT = "terminal_alert"
    OPERATION_FAILURE = "operation_failure"
    TEST = "test"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class NotificationSummary:
    intent_id: UUID
    source_event_id: UUID
    status: str
    counts: dict[str, int]


class AttemptOutcome(StrEnum):
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class NotificationChannelRecord:
    channel_id: UUID
    kind: ChannelKind
    name: str
    enabled: bool
    settings: dict[str, Any]
    secret_id: UUID | None
    bot_service_id: UUID | None
    row_version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class NotificationRouteRecord:
    route_id: UUID
    notification_kind: NotificationKind
    channel_id: UUID
    targets: tuple[str, ...]
    template_key: str
    enabled: bool
    row_version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class EnabledNotificationRoute:
    route: NotificationRouteRecord
    channel_kind: ChannelKind
    bot_service_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class NotificationIntentRecord:
    intent_id: UUID
    source_event_id: UUID
    notification_kind: NotificationKind
    source_type: str
    source_id: UUID
    correlation_id: UUID
    schema_version: int
    payload: dict[str, Any]
    disposition: str
    occurred_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NewNotificationDelivery:
    delivery_id: UUID
    intent_id: UUID
    route_id: UUID | None
    channel_id: UUID
    channel_kind: ChannelKind
    targets: tuple[str, ...]
    template_key: str
    available_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimedNotificationDelivery:
    delivery_id: UUID
    intent_id: UUID
    channel_id: UUID
    channel_kind: ChannelKind
    notification_kind: NotificationKind
    targets: tuple[str, ...]
    template_key: str
    payload: dict[str, Any]
    channel_settings: dict[str, Any]
    bot_service_id: UUID | None
    encrypted_secret: str | None
    secret_key_id: str | None
    attempt_count: int
    row_version: int
    started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    delivery_id: UUID
    kind: NotificationKind
    targets: tuple[str, ...]
    template_key: str
    title: str
    body: str
    payload: dict[str, Any]
    channel_settings: dict[str, Any]
    bot_service_id: UUID | None
    secret: str | None
    image_png: bytes | None = None


@dataclass(frozen=True, slots=True)
class AdapterReceipt:
    provider_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    delivery_id: UUID
    row_version: int
    attempt_count: int
    outcome: AttemptOutcome
    retryable: bool
    error_type: str | None = None
    error_code: str | None = None
    provider_message_id: str | None = None
    started_at: datetime | None = None
    evidence_pending: bool = False


@dataclass(frozen=True, slots=True)
class NotificationDispatchResult:
    claimed: int
    sent: int
    failed: int
    dead_lettered: int
    stale: int


@dataclass(frozen=True, slots=True)
class NotificationDeliveryView:
    delivery_id: UUID
    intent_id: UUID
    notification_kind: NotificationKind
    source_type: str
    source_id: UUID
    channel_id: UUID
    channel_kind: ChannelKind
    channel_name: str
    targets: tuple[str, ...]
    template_key: str
    status: DeliveryStatus
    attempt_count: int
    available_at: datetime
    last_error_type: str | None
    last_error_code: str | None
    created_at: datetime
    sent_at: datetime | None
    dead_lettered_at: datetime | None
    cancelled_at: datetime | None


@dataclass(frozen=True, slots=True)
class NotificationAttemptRecord:
    attempt_id: UUID
    delivery_id: UUID
    attempt_no: int
    outcome: AttemptOutcome
    retryable: bool
    error_type: str | None
    error_code: str | None
    provider_message_id: str | None
    started_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class QueuedNotificationTest:
    intent_id: UUID
    delivery_id: UUID
    replayed: bool
