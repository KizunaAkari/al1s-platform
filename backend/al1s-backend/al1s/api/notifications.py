from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Header, Query, Request, Response, status
from pydantic import AwareDatetime, BaseModel, Field

from al1s.api.cursors import decode_timestamp_cursor, encode_timestamp_cursor
from al1s.app.service_state import service_state
from al1s.notifications.errors import NotificationDomainError
from al1s.notifications.query_service import NotificationQueryService
from al1s.notifications.service import NotificationService
from al1s.notifications.types import (
    AttemptOutcome,
    ChannelKind,
    DeliveryStatus,
    NotificationAttemptRecord,
    NotificationChannelRecord,
    NotificationDeliveryView,
    NotificationKind,
    NotificationRouteRecord,
    SecretChange,
)

router = APIRouter()


class CreateChannelRequest(BaseModel):
    kind: ChannelKind
    name: str = Field(min_length=1, max_length=100)
    settings: dict[str, Any]
    secret: str | None = Field(default=None, max_length=4_096)
    bot_service_id: UUID | None = None


class ChannelResponse(BaseModel):
    channel_id: UUID
    kind: ChannelKind
    name: str
    enabled: bool
    settings: dict[str, Any]
    secret_configured: bool
    bot_service_id: UUID | None
    row_version: int
    created_at: datetime
    updated_at: datetime


class ChannelPageResponse(BaseModel):
    items: list[ChannelResponse]
    next_cursor: str | None


class UpdateChannelRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    settings: dict[str, Any] | None = None
    enabled: bool | None = None
    secret_change: SecretChange = SecretChange.PRESERVE
    secret: str | None = Field(default=None, max_length=4_096)
    bot_service_id: UUID | None = None


class CreateRouteRequest(BaseModel):
    notification_kind: NotificationKind
    channel_id: UUID
    targets: list[str] = Field(min_length=1, max_length=50)
    template_key: str = Field(min_length=1, max_length=100)


class RouteResponse(BaseModel):
    route_id: UUID
    notification_kind: NotificationKind
    channel_id: UUID
    targets: list[str]
    template_key: str
    enabled: bool
    row_version: int
    created_at: datetime
    updated_at: datetime


class RoutePageResponse(BaseModel):
    items: list[RouteResponse]
    next_cursor: str | None


class UpdateRouteRequest(BaseModel):
    targets: list[str] | None = Field(default=None, min_length=1, max_length=50)
    template_key: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None


class EnqueueTestRequest(BaseModel):
    channel_id: UUID
    targets: list[str] = Field(min_length=1, max_length=50)
    summary: str = Field(min_length=1, max_length=2_000)


class EnqueueTestResponse(BaseModel):
    intent_id: UUID
    delivery_id: UUID
    replayed: bool


class SubmitNotificationRequest(BaseModel):
    source_event_id: UUID
    source_type: str = Field(min_length=1, max_length=100)
    source_id: UUID
    notification_kind: Literal[
        "script_failure", "conditional_skip", "storage_low", "terminal_alert"
    ]
    occurred_at: AwareDatetime
    summary: str = Field(min_length=1, max_length=2000)


@router.post("/intents", status_code=status.HTTP_202_ACCEPTED)
def submit_notification(body: SubmitNotificationRequest, request: Request) -> dict[str, object]:
    intent = _commands(request).materialize_intent(
        source_event_id=body.source_event_id,
        source_type=body.source_type,
        source_id=body.source_id,
        notification_kind=NotificationKind(body.notification_kind),
        occurred_at=body.occurred_at,
        payload={"summary": body.summary},
        correlation_id=_correlation_id(request),
    )
    return {
        "intent_id": str(intent.intent_id),
        "source_event_id": str(intent.source_event_id),
        "disposition": intent.disposition,
    }


class DeliveryResponse(BaseModel):
    delivery_id: UUID
    intent_id: UUID
    notification_kind: NotificationKind
    source_type: str
    source_id: UUID
    channel_id: UUID
    channel_kind: ChannelKind
    channel_name: str
    targets: list[str]
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


class DeliveryPageResponse(BaseModel):
    items: list[DeliveryResponse]
    next_cursor: str | None


class AttemptResponse(BaseModel):
    attempt_id: UUID
    attempt_no: int
    outcome: AttemptOutcome
    retryable: bool
    error_type: str | None
    error_code: str | None
    provider_message_id: str | None
    started_at: datetime
    completed_at: datetime


class DeliveryDetailResponse(BaseModel):
    delivery: DeliveryResponse
    attempts: list[AttemptResponse]


class SourceSummaryResponse(BaseModel):
    intent_id: UUID
    source_event_id: UUID
    status: str
    counts: dict[str, int]


@router.get("/sources/{source_event_id}", response_model=SourceSummaryResponse)
def source_summary(source_event_id: UUID, request: Request) -> SourceSummaryResponse:
    summary = _queries(request).source_summary(source_event_id)
    return SourceSummaryResponse(
        intent_id=summary.intent_id,
        source_event_id=summary.source_event_id,
        status=summary.status,
        counts=summary.counts,
    )


def _commands(request: Request) -> NotificationService:
    return service_state(request).notification_commands


def _queries(request: Request) -> NotificationQueryService:
    return service_state(request).notification_queries


def _correlation_id(request: Request) -> UUID:
    try:
        return UUID(request.state.request_id)
    except ValueError:
        return UUID(int=0)


def _resource_id(operation: str, idempotency_key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"https://al1s.local/{operation}/{idempotency_key}")


def _expected_version(if_match: str) -> int:
    value = if_match.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    value = value.strip('"')
    try:
        parsed = int(value)
    except ValueError as error:
        raise NotificationDomainError(
            "invalid_if_match", "If-Match must contain a positive row version"
        ) from error
    if parsed < 1:
        raise NotificationDomainError(
            "invalid_if_match", "If-Match must contain a positive row version"
        )
    return parsed


def _channel(item: NotificationChannelRecord) -> ChannelResponse:
    return ChannelResponse(
        channel_id=item.channel_id,
        kind=item.kind,
        name=item.name,
        enabled=item.enabled,
        settings=dict(item.settings),
        secret_configured=item.secret_id is not None,
        bot_service_id=item.bot_service_id,
        row_version=item.row_version,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _route(item: NotificationRouteRecord) -> RouteResponse:
    return RouteResponse(
        route_id=item.route_id,
        notification_kind=item.notification_kind,
        channel_id=item.channel_id,
        targets=list(item.targets),
        template_key=item.template_key,
        enabled=item.enabled,
        row_version=item.row_version,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _delivery(item: NotificationDeliveryView) -> DeliveryResponse:
    return DeliveryResponse(
        delivery_id=item.delivery_id,
        intent_id=item.intent_id,
        notification_kind=item.notification_kind,
        source_type=item.source_type,
        source_id=item.source_id,
        channel_id=item.channel_id,
        channel_kind=item.channel_kind,
        channel_name=item.channel_name,
        targets=list(item.targets),
        template_key=item.template_key,
        status=item.status,
        attempt_count=item.attempt_count,
        available_at=item.available_at,
        last_error_type=item.last_error_type,
        last_error_code=item.last_error_code,
        created_at=item.created_at,
        sent_at=item.sent_at,
        dead_lettered_at=item.dead_lettered_at,
        cancelled_at=item.cancelled_at,
    )


def _attempt(item: NotificationAttemptRecord) -> AttemptResponse:
    return AttemptResponse(
        attempt_id=item.attempt_id,
        attempt_no=item.attempt_no,
        outcome=item.outcome,
        retryable=item.retryable,
        error_type=item.error_type,
        error_code=item.error_code,
        provider_message_id=item.provider_message_id,
        started_at=item.started_at,
        completed_at=item.completed_at,
    )


@router.post("/channels", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
def create_channel(
    body: CreateChannelRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> ChannelResponse:
    item = _commands(request).create_channel(
        kind=body.kind,
        name=body.name,
        settings=body.settings,
        secret=body.secret,
        bot_service_id=body.bot_service_id,
        actor_id=None,
        correlation_id=_correlation_id(request),
        channel_id=_resource_id("notification-channel", idempotency_key),
    )
    return _channel(item)


@router.get("/channels", response_model=ChannelPageResponse)
def list_channels(
    request: Request,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ChannelPageResponse:
    before_created_at, before_id = decode_timestamp_cursor(cursor)
    items = _queries(request).list_channels(
        before_created_at=before_created_at,
        before_id=before_id,
        limit=limit,
    )
    return ChannelPageResponse(
        items=[_channel(item) for item in items],
        next_cursor=(
            encode_timestamp_cursor(items[-1].created_at, items[-1].channel_id)
            if len(items) == limit
            else None
        ),
    )


@router.patch("/channels/{channel_id}", response_model=ChannelResponse)
def update_channel(
    channel_id: UUID,
    body: UpdateChannelRequest,
    request: Request,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=32)],
) -> ChannelResponse:
    item = _commands(request).update_channel(
        channel_id=channel_id,
        expected_version=_expected_version(if_match),
        name=body.name,
        settings=body.settings,
        enabled=body.enabled,
        bot_service_id=body.bot_service_id,
        secret_change=body.secret_change,
        secret=body.secret,
        actor_id=None,
        correlation_id=_correlation_id(request),
    )
    response.headers["ETag"] = f'"{item.row_version}"'
    return _channel(item)


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_channel(
    channel_id: UUID,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=32)],
) -> None:
    _commands(request).delete_channel(
        channel_id=channel_id,
        expected_version=_expected_version(if_match),
        actor_id=None,
        correlation_id=_correlation_id(request),
    )


@router.post("/routes", response_model=RouteResponse, status_code=status.HTTP_201_CREATED)
def create_route(
    body: CreateRouteRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> RouteResponse:
    item = _commands(request).create_route(
        notification_kind=body.notification_kind,
        channel_id=body.channel_id,
        targets=body.targets,
        template_key=body.template_key,
        actor_id=None,
        correlation_id=_correlation_id(request),
        route_id=_resource_id("notification-route", idempotency_key),
    )
    return _route(item)


@router.get("/routes", response_model=RoutePageResponse)
def list_routes(
    request: Request,
    notification_kind: Annotated[NotificationKind | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RoutePageResponse:
    before_created_at, before_id = decode_timestamp_cursor(cursor)
    items = _queries(request).list_routes(
        notification_kind=notification_kind,
        before_created_at=before_created_at,
        before_id=before_id,
        limit=limit,
    )
    return RoutePageResponse(
        items=[_route(item) for item in items],
        next_cursor=(
            encode_timestamp_cursor(items[-1].created_at, items[-1].route_id)
            if len(items) == limit
            else None
        ),
    )


@router.patch("/routes/{route_id}", response_model=RouteResponse)
def update_route(
    route_id: UUID,
    body: UpdateRouteRequest,
    request: Request,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=32)],
) -> RouteResponse:
    item = _commands(request).update_route(
        route_id=route_id,
        expected_version=_expected_version(if_match),
        targets=body.targets,
        template_key=body.template_key,
        enabled=body.enabled,
        actor_id=None,
        correlation_id=_correlation_id(request),
    )
    response.headers["ETag"] = f'"{item.row_version}"'
    return _route(item)


@router.post("/tests", response_model=EnqueueTestResponse, status_code=status.HTTP_202_ACCEPTED)
def enqueue_test(
    body: EnqueueTestRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> EnqueueTestResponse:
    result = _commands(request).enqueue_test(
        source_event_id=_resource_id("notification-test", idempotency_key),
        channel_id=body.channel_id,
        targets=body.targets,
        summary=body.summary,
        correlation_id=_correlation_id(request),
    )
    return EnqueueTestResponse(
        intent_id=result.intent_id,
        delivery_id=result.delivery_id,
        replayed=result.replayed,
    )


@router.get("/deliveries", response_model=DeliveryPageResponse)
def list_deliveries(
    request: Request,
    delivery_status: Annotated[DeliveryStatus | None, Query(alias="status")] = None,
    notification_kind: Annotated[NotificationKind | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> DeliveryPageResponse:
    before_created_at, before_id = decode_timestamp_cursor(cursor)
    items = _queries(request).list_deliveries(
        status=delivery_status,
        notification_kind=notification_kind,
        before_created_at=before_created_at,
        before_id=before_id,
        limit=limit,
    )
    return DeliveryPageResponse(
        items=[_delivery(item) for item in items],
        next_cursor=(
            encode_timestamp_cursor(items[-1].created_at, items[-1].delivery_id)
            if len(items) == limit
            else None
        ),
    )


@router.get("/deliveries/{delivery_id}", response_model=DeliveryDetailResponse)
def get_delivery(delivery_id: UUID, request: Request) -> DeliveryDetailResponse:
    delivery, attempts = _queries(request).get_delivery(delivery_id)
    return DeliveryDetailResponse(
        delivery=_delivery(delivery), attempts=[_attempt(item) for item in attempts]
    )
