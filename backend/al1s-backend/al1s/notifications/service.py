from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from al1s.bots.types import BotServiceKind
from al1s.kernel.types import NewAuditEntry, NewOutboxEvent
from al1s.notifications.errors import NotificationDomainError
from al1s.notifications.ports import NotificationUnitOfWork
from al1s.notifications.types import (
    ChannelKind,
    NewNotificationDelivery,
    NotificationChannelRecord,
    NotificationIntentRecord,
    NotificationKind,
    NotificationRouteRecord,
    QueuedNotificationTest,
    SecretChange,
)
from al1s.secrets.ports import SecretCipher
from al1s.secrets.types import SecretRecord

UowFactory = Callable[[], NotificationUnitOfWork]

ALLOWED_ROUTE_CHANNELS: dict[NotificationKind, frozenset[ChannelKind]] = {
    NotificationKind.CONDITIONAL_SKIP: frozenset({ChannelKind.QQ}),
    NotificationKind.SCRIPT_FAILURE: frozenset({ChannelKind.QQ, ChannelKind.SMTP}),
    NotificationKind.STORAGE_LOW: frozenset({ChannelKind.QQ, ChannelKind.SMTP}),
    NotificationKind.TERMINAL_ALERT: frozenset(ChannelKind),
    NotificationKind.OPERATION_FAILURE: frozenset({ChannelKind.QQ, ChannelKind.SMTP}),
    NotificationKind.TEST: frozenset(ChannelKind),
}


class NotificationService:
    def __init__(
        self,
        uow_factory: UowFactory,
        cipher: SecretCipher,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._cipher = cipher
        self._now = now or (lambda: datetime.now(UTC))

    def create_channel(
        self,
        *,
        kind: ChannelKind,
        name: str,
        settings: dict[str, Any],
        secret: str | None,
        bot_service_id: UUID | None,
        actor_id: UUID | None,
        correlation_id: UUID,
        channel_id: UUID | None = None,
    ) -> NotificationChannelRecord:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 100:
            raise NotificationDomainError("invalid_channel_name", "Channel name is invalid")
        _validate_channel_settings(kind, settings)
        if kind is not ChannelKind.SMTP and secret is not None:
            raise NotificationDomainError(
                "bot_secret_owned_elsewhere",
                "QQ and Discord secrets are managed by the Bot service boundary",
            )
        now = self._now()
        resolved_channel_id = channel_id or uuid4()
        secret_record = None
        if secret:
            secret_record = SecretRecord(
                secret_id=uuid4(),
                purpose=f"notification_channel:{resolved_channel_id}",
                ciphertext=self._cipher.encrypt(secret),
                key_id=self._cipher.key_id,
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        record = NotificationChannelRecord(
            channel_id=resolved_channel_id,
            kind=kind,
            name=normalized_name,
            enabled=True,
            settings=dict(settings),
            secret_id=secret_record.secret_id if secret_record else None,
            bot_service_id=bot_service_id,
            row_version=1,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        with self._uow_factory() as uow:
            _validate_bot_service_binding(uow, kind, bot_service_id)
            if secret_record is not None:
                uow.secrets.add(secret_record)
            if not uow.channels.add(record):
                existing = uow.channels.get(resolved_channel_id)
                if existing is None:
                    raise NotificationDomainError(
                        "notification_channel_conflict",
                        "Channel name already exists",
                        409,
                    )
                if not _channel_matches(uow, existing, record, secret, self._cipher):
                    raise NotificationDomainError(
                        "idempotency_key_conflict",
                        "Idempotency key was already used with another request",
                        409,
                    )
                return existing
            _record_management_event(
                uow,
                event_type="notification.channel.created.v1",
                aggregate_type="notification_channel",
                aggregate_id=resolved_channel_id,
                actor_id=actor_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={"channel_id": str(resolved_channel_id), "kind": kind.value},
            )
            uow.commit()
        return record

    def create_route(
        self,
        *,
        notification_kind: NotificationKind,
        channel_id: UUID,
        targets: Sequence[str],
        template_key: str,
        actor_id: UUID | None,
        correlation_id: UUID,
        route_id: UUID | None = None,
    ) -> NotificationRouteRecord:
        if notification_kind is NotificationKind.FORWARD:
            raise NotificationDomainError(
                "legacy_forward_retired", "Use Discord message rules for forwarding", 409
            )
        normalized_targets = tuple(dict.fromkeys(item.strip() for item in targets if item.strip()))
        if not normalized_targets or len(normalized_targets) > 50:
            raise NotificationDomainError("invalid_route_targets", "Route targets are invalid")
        if not template_key or len(template_key) > 100:
            raise NotificationDomainError("invalid_template_key", "Template key is invalid")
        now = self._now()
        with self._uow_factory() as uow:
            channel = uow.channels.get(channel_id, for_update=True)
            if channel is None or channel.deleted_at is not None:
                raise NotificationDomainError(
                    "notification_channel_not_found", "Channel not found", 404
                )
            if channel.kind not in ALLOWED_ROUTE_CHANNELS[notification_kind]:
                raise NotificationDomainError(
                    "notification_route_channel_forbidden",
                    f"{channel.kind.value} is not allowed for {notification_kind.value}",
                )
            resolved_route_id = route_id or uuid4()
            route = NotificationRouteRecord(
                route_id=resolved_route_id,
                notification_kind=notification_kind,
                channel_id=channel_id,
                targets=normalized_targets,
                template_key=template_key,
                enabled=True,
                row_version=1,
                created_at=now,
                updated_at=now,
                deleted_at=None,
            )
            if not uow.routes.add(route):
                existing = uow.routes.get(resolved_route_id)
                if existing is None:
                    raise NotificationDomainError(
                        "notification_route_conflict",
                        "An equivalent route already exists",
                        409,
                    )
                if not _route_matches(existing, route):
                    raise NotificationDomainError(
                        "idempotency_key_conflict",
                        "Idempotency key was already used with another request",
                        409,
                    )
                return existing
            _record_management_event(
                uow,
                event_type="notification.route.created.v1",
                aggregate_type="notification_route",
                aggregate_id=route.route_id,
                actor_id=actor_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "route_id": str(route.route_id),
                    "notification_kind": notification_kind.value,
                    "channel_id": str(channel_id),
                },
            )
            uow.commit()
        return route

    def update_channel(
        self,
        *,
        channel_id: UUID,
        expected_version: int,
        name: str | None,
        settings: dict[str, Any] | None,
        enabled: bool | None,
        bot_service_id: UUID | None,
        secret_change: SecretChange,
        secret: str | None,
        actor_id: UUID | None,
        correlation_id: UUID,
    ) -> NotificationChannelRecord:
        if expected_version < 1:
            raise NotificationDomainError(
                "invalid_row_version", "Expected row version must be positive"
            )
        if (
            name is None
            and settings is None
            and enabled is None
            and bot_service_id is None
            and secret_change is SecretChange.PRESERVE
        ):
            raise NotificationDomainError(
                "no_notification_channel_changes", "No channel changes were supplied"
            )
        now = self._now()
        with self._uow_factory() as uow:
            current = uow.channels.get(channel_id, for_update=True)
            if current is None or current.deleted_at is not None:
                raise NotificationDomainError(
                    "notification_channel_not_found", "Channel not found", 404
                )
            _ensure_expected_version(current.row_version, expected_version)
            normalized_name = current.name if name is None else name.strip()
            if not normalized_name or len(normalized_name) > 100:
                raise NotificationDomainError("invalid_channel_name", "Channel name is invalid")
            resolved_settings = current.settings if settings is None else dict(settings)
            _validate_channel_settings(current.kind, resolved_settings)
            resolved_bot_service_id = current.bot_service_id
            if bot_service_id is not None:
                resolved_bot_service_id = bot_service_id
            _validate_bot_service_binding(uow, current.kind, resolved_bot_service_id)
            next_secret_id = current.secret_id
            if secret_change is SecretChange.SET:
                if current.kind is not ChannelKind.SMTP:
                    raise NotificationDomainError(
                        "bot_secret_owned_elsewhere",
                        "QQ and Discord secrets are managed by the Bot service boundary",
                    )
                if secret is None or not secret:
                    raise NotificationDomainError(
                        "notification_secret_required", "A non-empty secret is required"
                    )
                secret_record = SecretRecord(
                    secret_id=uuid4(),
                    purpose=f"notification_channel:{channel_id}",
                    ciphertext=self._cipher.encrypt(secret),
                    key_id=self._cipher.key_id,
                    row_version=1,
                    created_at=now,
                    updated_at=now,
                )
                uow.secrets.add(secret_record)
                next_secret_id = secret_record.secret_id
            elif secret_change is SecretChange.CLEAR:
                if secret is not None:
                    raise NotificationDomainError(
                        "unexpected_notification_secret",
                        "Secret value must be omitted when clearing a secret",
                    )
                next_secret_id = None
            elif secret is not None:
                raise NotificationDomainError(
                    "unexpected_notification_secret",
                    "Use secret_change=set to replace a secret",
                )
            updated = replace(
                current,
                name=normalized_name,
                enabled=current.enabled if enabled is None else enabled,
                settings=resolved_settings,
                secret_id=next_secret_id,
                bot_service_id=resolved_bot_service_id,
                row_version=current.row_version + 1,
                updated_at=now,
            )
            if not uow.channels.update(updated, expected_version=expected_version):
                raise NotificationDomainError(
                    "notification_channel_version_conflict",
                    "Channel was modified by another request",
                    409,
                )
            cancelled = 0
            if current.enabled and not updated.enabled:
                cancelled = uow.deliveries.cancel_pending_for_channel(channel_id, now=now)
            if current.secret_id is not None and current.secret_id != updated.secret_id:
                uow.secrets.delete_if_unreferenced(current.secret_id)
            _record_management_event(
                uow,
                event_type="notification.channel.updated.v1",
                aggregate_type="notification_channel",
                aggregate_id=channel_id,
                actor_id=actor_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "channel_id": str(channel_id),
                    "enabled": updated.enabled,
                    "cancelled_deliveries": cancelled,
                    "row_version": updated.row_version,
                },
            )
            uow.commit()
        return updated

    def delete_channel(
        self,
        *,
        channel_id: UUID,
        expected_version: int,
        actor_id: UUID | None,
        correlation_id: UUID,
    ) -> None:
        if expected_version < 1:
            raise NotificationDomainError(
                "invalid_row_version", "Expected row version must be positive"
            )
        now = self._now()
        with self._uow_factory() as uow:
            current = uow.channels.get(channel_id, for_update=True)
            if current is None or current.deleted_at is not None:
                raise NotificationDomainError(
                    "notification_channel_not_found", "Channel not found", 404
                )
            _ensure_expected_version(current.row_version, expected_version)
            deleted = replace(
                current,
                enabled=False,
                secret_id=None,
                deleted_at=now,
                updated_at=now,
                row_version=current.row_version + 1,
            )
            if not uow.channels.update(deleted, expected_version=expected_version):
                raise NotificationDomainError(
                    "notification_channel_version_conflict",
                    "Channel was modified by another request",
                    409,
                )
            deleted_routes = uow.routes.soft_delete_for_channel(channel_id, now=now)
            cancelled = uow.deliveries.cancel_pending_for_channel(channel_id, now=now)
            if current.secret_id is not None:
                uow.secrets.delete_if_unreferenced(current.secret_id)
            _record_management_event(
                uow,
                event_type="notification.channel.deleted.v1",
                aggregate_type="notification_channel",
                aggregate_id=channel_id,
                actor_id=actor_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "channel_id": str(channel_id),
                    "deleted_routes": deleted_routes,
                    "cancelled_deliveries": cancelled,
                    "row_version": deleted.row_version,
                },
            )
            uow.commit()

    def update_route(
        self,
        *,
        route_id: UUID,
        expected_version: int,
        targets: Sequence[str] | None,
        template_key: str | None,
        enabled: bool | None,
        actor_id: UUID | None,
        correlation_id: UUID,
    ) -> NotificationRouteRecord:
        if expected_version < 1:
            raise NotificationDomainError(
                "invalid_row_version", "Expected row version must be positive"
            )
        if targets is None and template_key is None and enabled is None:
            raise NotificationDomainError(
                "no_notification_route_changes", "No route changes were supplied"
            )
        normalized_targets = (
            None
            if targets is None
            else tuple(dict.fromkeys(item.strip() for item in targets if item.strip()))
        )
        if normalized_targets is not None and (
            not normalized_targets or len(normalized_targets) > 50
        ):
            raise NotificationDomainError("invalid_route_targets", "Route targets are invalid")
        normalized_template = None if template_key is None else template_key.strip()
        if normalized_template is not None and (
            not normalized_template or len(normalized_template) > 100
        ):
            raise NotificationDomainError("invalid_template_key", "Template key is invalid")
        now = self._now()
        with self._uow_factory() as uow:
            current = uow.routes.get(route_id, for_update=True)
            if current is None or current.deleted_at is not None:
                raise NotificationDomainError(
                    "notification_route_not_found", "Route not found", 404
                )
            _ensure_expected_version(current.row_version, expected_version)
            if current.notification_kind is NotificationKind.FORWARD and enabled is not False:
                raise NotificationDomainError(
                    "legacy_forward_retired", "Legacy forwarding route cannot be modified", 409
                )
            updated = replace(
                current,
                targets=current.targets if normalized_targets is None else normalized_targets,
                template_key=(
                    current.template_key if normalized_template is None else normalized_template
                ),
                enabled=current.enabled if enabled is None else enabled,
                row_version=current.row_version + 1,
                updated_at=now,
            )
            if not uow.routes.update(updated, expected_version=expected_version):
                raise NotificationDomainError(
                    "notification_route_version_conflict",
                    "Route was modified by another request",
                    409,
                )
            cancelled = 0
            if current.enabled and not updated.enabled:
                cancelled = uow.deliveries.cancel_pending_for_route(route_id, now=now)
            _record_management_event(
                uow,
                event_type="notification.route.updated.v1",
                aggregate_type="notification_route",
                aggregate_id=route_id,
                actor_id=actor_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "route_id": str(route_id),
                    "enabled": updated.enabled,
                    "cancelled_deliveries": cancelled,
                    "row_version": updated.row_version,
                },
            )
            uow.commit()
        return updated

    def materialize_intent(
        self,
        *,
        source_event_id: UUID,
        notification_kind: NotificationKind,
        source_type: str,
        source_id: UUID,
        correlation_id: UUID,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> NotificationIntentRecord:
        if notification_kind is NotificationKind.FORWARD:
            raise NotificationDomainError(
                "review_ingress_required", "Forwarded messages must use review ingress",
            )
        if not source_type or len(source_type) > 100:
            raise NotificationDomainError("invalid_notification_source", "Source type is invalid")
        expected = (notification_kind, source_type, source_id, payload, occurred_at)
        now = self._now()
        with self._uow_factory() as uow:
            existing = uow.intents.get_by_source_event(source_event_id)
            if existing is not None:
                _assert_source_replay(existing, expected)
                return existing
            routes = uow.routes.list_enabled_for_kind(notification_kind.value)
            intent = NotificationIntentRecord(
                intent_id=uuid4(),
                source_event_id=source_event_id,
                notification_kind=notification_kind,
                source_type=source_type,
                source_id=source_id,
                correlation_id=correlation_id,
                schema_version=1,
                payload=dict(payload),
                disposition="queued" if routes else "no_route",
                occurred_at=occurred_at,
                created_at=now,
            )
            if not uow.intents.add(intent):
                concurrent = uow.intents.get_by_source_event(source_event_id)
                if concurrent is None:
                    raise RuntimeError(
                        "notification intent conflict did not return existing record"
                    )
                _assert_source_replay(concurrent, expected)
                return concurrent
            deliveries = [
                NewNotificationDelivery(
                    delivery_id=uuid4(),
                    intent_id=intent.intent_id,
                    route_id=item.route.route_id,
                    channel_id=item.route.channel_id,
                    channel_kind=item.channel_kind,
                    targets=item.route.targets,
                    template_key=item.route.template_key,
                    available_at=now,
                )
                for item in routes
            ]
            uow.deliveries.add_many(deliveries)
            uow.commit()
        return intent

    def enqueue_test(
        self,
        *,
        source_event_id: UUID,
        channel_id: UUID,
        targets: Sequence[str],
        summary: str,
        correlation_id: UUID,
    ) -> QueuedNotificationTest:
        normalized_targets = tuple(dict.fromkeys(item.strip() for item in targets if item.strip()))
        if not normalized_targets or len(normalized_targets) > 50:
            raise NotificationDomainError("invalid_route_targets", "Test targets are invalid")
        if not summary.strip() or len(summary) > 2_000:
            raise NotificationDomainError("invalid_test_summary", "Test summary is invalid")
        now = self._now()
        with self._uow_factory() as uow:
            existing = uow.intents.get_by_source_event(source_event_id)
            if existing is not None:
                delivery = uow.deliveries.get_for_intent(existing.intent_id)
                if delivery is None:
                    raise RuntimeError("test intent exists without a delivery")
                _ensure_test_replay_matches(
                    existing,
                    delivery.channel_id,
                    delivery.targets,
                    channel_id,
                    normalized_targets,
                    summary.strip(),
                )
                return QueuedNotificationTest(existing.intent_id, delivery.delivery_id, True)
            channel = uow.channels.get(channel_id, for_update=True)
            if channel is None or channel.deleted_at is not None:
                raise NotificationDomainError(
                    "notification_channel_not_found", "Channel not found", 404
                )
            if not channel.enabled:
                raise NotificationDomainError(
                    "notification_channel_disabled", "Channel is disabled", 409
                )
            intent = NotificationIntentRecord(
                intent_id=uuid4(),
                source_event_id=source_event_id,
                notification_kind=NotificationKind.TEST,
                source_type="notification_channel",
                source_id=channel_id,
                correlation_id=correlation_id,
                schema_version=1,
                payload={"summary": summary.strip(), "targets": list(normalized_targets)},
                disposition="queued",
                occurred_at=now,
                created_at=now,
            )
            if not uow.intents.add(intent):
                concurrent = uow.intents.get_by_source_event(source_event_id)
                if concurrent is None:
                    raise RuntimeError("test intent conflict did not return existing record")
                delivery = uow.deliveries.get_for_intent(concurrent.intent_id)
                if delivery is None:
                    raise RuntimeError("concurrent test intent exists without a delivery")
                _ensure_test_replay_matches(
                    concurrent,
                    delivery.channel_id,
                    delivery.targets,
                    channel_id,
                    normalized_targets,
                    summary.strip(),
                )
                return QueuedNotificationTest(concurrent.intent_id, delivery.delivery_id, True)
            delivery_id = uuid4()
            uow.deliveries.add_many(
                [
                    NewNotificationDelivery(
                        delivery_id=delivery_id,
                        intent_id=intent.intent_id,
                        route_id=None,
                        channel_id=channel.channel_id,
                        channel_kind=channel.kind,
                        targets=normalized_targets,
                        template_key="test.v1",
                        available_at=now,
                    )
                ]
            )
            uow.commit()
        # A multi-target request materializes several deliveries; use the same stable
        # representative as an idempotent replay rather than the first generated UUID.
        with self._uow_factory() as uow:
            representative = uow.deliveries.get_for_intent(intent.intent_id)
            if representative is None:
                raise RuntimeError("test intent exists without a delivery")
        return QueuedNotificationTest(intent.intent_id, representative.delivery_id, False)


def _assert_source_replay(existing: NotificationIntentRecord, expected: tuple[object, ...]) -> None:
    actual = (
        existing.notification_kind, existing.source_type, existing.source_id,
        existing.payload, existing.occurred_at,
    )
    if actual != expected:
        raise NotificationDomainError(
            "notification_source_conflict",
            "Source event already exists with different content",
            status_code=409,
        )


def _validate_channel_settings(kind: ChannelKind, settings: dict[str, Any]) -> None:
    if kind is ChannelKind.SMTP:
        host = settings.get("host")
        port = settings.get("port")
        sender = settings.get("from_address")
        security = settings.get("security")
        if not isinstance(host, str) or not host.strip() or len(host) > 255:
            raise NotificationDomainError("invalid_smtp_host", "SMTP host is invalid")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise NotificationDomainError("invalid_smtp_port", "SMTP port is invalid")
        if not isinstance(sender, str) or "@" not in sender or len(sender) > 320:
            raise NotificationDomainError("invalid_smtp_sender", "SMTP sender is invalid")
        if security not in {"starttls", "ssl", "plain"}:
            raise NotificationDomainError("invalid_smtp_security", "SMTP security mode is invalid")
        username = settings.get("username")
        if username is not None and (not isinstance(username, str) or len(username.strip()) > 320):
            raise NotificationDomainError("invalid_smtp_username", "SMTP username is invalid")
        timeout = settings.get("timeout_seconds", 20)
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not 1 <= timeout <= 60
        ):
            raise NotificationDomainError("invalid_smtp_timeout", "SMTP timeout is invalid")
    else:
        try:
            encoded = json.dumps(settings, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise NotificationDomainError(
                "invalid_notification_channel_settings", "Channel settings must be valid JSON"
            ) from exc
        if len(encoded.encode("utf-8")) > 16_384:
            raise NotificationDomainError(
                "notification_channel_settings_too_large", "Channel settings exceed 16 KiB"
            )


def _validate_bot_service_binding(
    uow: NotificationUnitOfWork,
    kind: ChannelKind,
    bot_service_id: UUID | None,
) -> None:
    expected_kind = {
        ChannelKind.QQ: BotServiceKind.ONEBOT_GATEWAY,
        ChannelKind.DISCORD: BotServiceKind.DISCORD_BRIDGE,
    }.get(kind)
    if expected_kind is None:
        if bot_service_id is not None:
            raise NotificationDomainError(
                "smtp_channel_cannot_bind_bot", "SMTP channel cannot bind a Bot service"
            )
        return
    if bot_service_id is None:
        raise NotificationDomainError(
            "bot_service_binding_required", f"{kind.value} channel requires a Bot service"
        )
    service = uow.bot_services.get(bot_service_id)
    if (
        service is None
        or service.deleted_at is not None
        or not service.enabled
        or service.kind is not expected_kind
    ):
        raise NotificationDomainError(
            "invalid_bot_service_binding",
            f"{kind.value} channel must reference an enabled {expected_kind.value} service",
        )


def _ensure_expected_version(actual: int, expected: int) -> None:
    if actual != expected:
        raise NotificationDomainError(
            "notification_version_conflict",
            f"Expected row version {expected}, but current version is {actual}",
            409,
        )


def _channel_matches(
    uow: NotificationUnitOfWork,
    existing: NotificationChannelRecord,
    requested: NotificationChannelRecord,
    requested_secret: str | None,
    cipher: SecretCipher,
) -> bool:
    if (
        existing.kind != requested.kind
        or existing.name != requested.name
        or existing.settings != requested.settings
        or existing.bot_service_id != requested.bot_service_id
        or existing.deleted_at is not None
    ):
        return False
    if not requested_secret:
        return existing.secret_id is None
    if existing.secret_id is None:
        return False
    stored = uow.secrets.get(existing.secret_id)
    return stored is not None and requested_secret == cipher.decrypt(
        stored.ciphertext, stored.key_id
    )


def _route_matches(existing: NotificationRouteRecord, requested: NotificationRouteRecord) -> bool:
    return (
        existing.notification_kind == requested.notification_kind
        and existing.channel_id == requested.channel_id
        and existing.targets == requested.targets
        and existing.template_key == requested.template_key
        and existing.deleted_at is None
    )


def _ensure_test_replay_matches(
    intent: NotificationIntentRecord,
    actual_channel_id: UUID,
    actual_targets: tuple[str, ...],
    requested_channel_id: UUID,
    requested_targets: tuple[str, ...],
    requested_summary: str,
) -> None:
    if (
        intent.notification_kind is not NotificationKind.TEST
        or intent.source_type != "notification_channel"
        or intent.source_id != requested_channel_id
        or actual_channel_id != requested_channel_id
        or any(target not in requested_targets for target in actual_targets)
        or intent.payload != {"summary": requested_summary, "targets": list(requested_targets)}
    ):
        raise NotificationDomainError(
            "idempotency_key_conflict",
            "Idempotency key was already used with another request",
            409,
        )


def _record_management_event(
    uow: NotificationUnitOfWork,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    actor_id: UUID | None,
    correlation_id: UUID,
    occurred_at: datetime,
    payload: dict[str, Any],
) -> None:
    uow.audit.add(
        NewAuditEntry(
            audit_id=uuid4(),
            actor_type="user" if actor_id else "system",
            actor_id=actor_id,
            action=event_type.removesuffix(".v1"),
            target_type=aggregate_type,
            target_id=aggregate_id,
            correlation_id=correlation_id,
            details=dict(payload),
        )
    )
    uow.outbox.add(
        NewOutboxEvent(
            event_id=uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
            payload=dict(payload),
        )
    )
