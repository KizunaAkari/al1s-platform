"""Read current account facts separately from container liveness."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from al1s.bots.runtime_connection import BotConnectionReader
from al1s.bots.service import BotService
from al1s.bots.types import BotServiceKind
from al1s.infrastructure.direct_http import post_json_direct

RuntimeState = Literal["online", "offline", "unknown", "disabled", "unconfigured"]
ERROR_CODES = frozenset(
    {
        "discord_login_timeout",
        "discord_authentication_failed",
        "discord_connection_error",
        "discord_gateway_disconnected",
        "discord_client_error",
    }
)


@dataclass(frozen=True)
class BotRuntimeStatus:
    service_id: UUID
    state: RuntimeState
    checked_at: datetime
    observed_at: datetime | None
    reason_code: str
    error_code: str | None = None


class BotRuntimeService:
    def __init__(
        self,
        services: BotService,
        connections: BotConnectionReader,
        *,
        now: Callable[[], datetime] | None = None,
        post: Callable[..., object] = post_json_direct,
    ):
        self._services, self._connections, self._post = services, connections, post
        self._now = now or (lambda: datetime.now(UTC))

    def read(self, service_id: UUID) -> BotRuntimeStatus:
        service = self._services.get_service(service_id)
        now = self._now()
        if not service.enabled:
            return BotRuntimeStatus(service_id, "disabled", now, None, "disabled")
        if service.applied_config_version_id is None:
            return BotRuntimeStatus(service_id, "unconfigured", now, None, "unconfigured")
        if service.kind == BotServiceKind.ONEBOT_GATEWAY:
            return self._qq(service_id, now)
        return self._discord(service_id, service.applied_config_version_id, now)

    def _qq(self, service_id: UUID, now: datetime) -> BotRuntimeStatus:
        try:
            connection = self._connections.read(service_id, BotServiceKind.ONEBOT_GATEWAY)
            base = str(connection.settings.get("ONEBOT_BASE_URL", ""))
            parsed = urlsplit(base)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("invalid_gateway_url")
            value = self._post(
                base.rstrip("/") + "/get_status", connection.secret, {}, timeout_seconds=3
            )
        except Exception:
            return BotRuntimeStatus(service_id, "unknown", now, None, "qq_status_unavailable")
        if not isinstance(value, dict):
            return BotRuntimeStatus(service_id, "unknown", now, None, "qq_status_invalid")
        data = value.get("data")
        if (
            value.get("status") != "ok"
            or type(value.get("retcode")) is not int
            or value["retcode"] != 0
            or not isinstance(data, dict)
            or type(data.get("online")) is not bool
            or type(data.get("good")) is not bool
        ):
            return BotRuntimeStatus(service_id, "unknown", now, None, "qq_status_invalid")
        reason = (
            "online"
            if data["online"] and data["good"]
            else ("qq_not_logged_in" if not data["online"] else "qq_gateway_unhealthy")
        )
        return BotRuntimeStatus(
            service_id, "online" if reason == "online" else "offline", now, now, reason
        )

    def _discord(self, service_id: UUID, version_id: UUID, now: datetime) -> BotRuntimeStatus:
        reports = self._services.list_health_reports(
            service_id, before_reported_at=None, before_id=None, limit=1
        )
        if not reports:
            return BotRuntimeStatus(service_id, "unknown", now, None, "discord_heartbeat_missing")
        report = reports[0]
        error = report.diagnostics.get("runtime_error_code")
        error = error if isinstance(error, str) and error in ERROR_CODES else None
        reason = "online"
        if (now - report.reported_at).total_seconds() > 90:
            reason = "discord_heartbeat_stale"
        elif report.config_version_id != version_id:
            reason = "discord_config_mismatch"
        elif report.diagnostics.get("shutting_down") is True:
            reason = "discord_stopping"
        elif report.diagnostics.get("discord_ready") is not True:
            reason = "discord_disconnected"
        return BotRuntimeStatus(
            service_id,
            "online" if reason == "online" else "offline",
            now,
            report.reported_at,
            reason,
            error,
        )
