from collections.abc import Callable
from uuid import UUID, uuid4

from al1s.bots.errors import BotDomainError
from al1s.bots.ports import BotUnitOfWork
from al1s.bots.types import BotConfigApplicationStatus, BotServiceKind
from al1s.infrastructure.direct_http import post_json_direct
from al1s.kernel.types import NewAuditEntry
from al1s.secrets.ports import SecretCipher


class GatewayProbe:
    def __init__(self, uow: Callable[[], BotUnitOfWork], cipher: SecretCipher):
        self._uow = uow
        self._cipher = cipher

    def apply(self, service_id: UUID) -> dict[str, object]:
        from datetime import UTC, datetime
        from urllib.parse import urlsplit

        with self._uow() as uow:
            service = uow.services.get(service_id)
            if not service or not service.enabled or service.kind != BotServiceKind.ONEBOT_GATEWAY:
                raise BotDomainError("gateway_not_found", "Enabled gateway required", 404)
            pending = uow.configs.get_pending_for_service(service_id)
            if pending is None:
                raise BotDomainError("gateway_no_candidate", "No pending configuration", 409)
            version = uow.configs.get_version(pending.config_version_id)
            if version is None:
                raise BotDomainError("gateway_config_missing", "Configuration missing", 409)
            secret = uow.secrets.get(version.secret_id) if version.secret_id else None
            token = self._cipher.decrypt(secret.ciphertext, secret.key_id) if secret else None
            base = str(version.settings.get("ONEBOT_BASE_URL", ""))
        url = urlsplit(base)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise BotDomainError("gateway_invalid_url", "Invalid gateway URL")
        try:
            result = post_json_direct(base.rstrip("/") + "/get_login_info", token, {})
            valid = (
                isinstance(result, dict)
                and result.get("status") == "ok"
                and type(result.get("retcode")) is int
                and result["retcode"] == 0
                and isinstance(result.get("data"), dict)
                and type(result["data"].get("user_id")) is int
                and result["data"]["user_id"] > 0
            )
        except Exception:
            valid = False
        now = datetime.now(UTC)
        with self._uow() as uow:
            service = uow.services.get(service_id, for_update=True)
            if (
                service is None
                or not service.enabled
                or service.desired_config_version_id != pending.config_version_id
            ):
                raise BotDomainError("gateway_candidate_changed", "Candidate changed", 409)
            current = uow.configs.get_application(pending.application_id, for_update=True)
            if current is None or current.status != BotConfigApplicationStatus.PENDING:
                raise BotDomainError("gateway_candidate_changed", "Candidate changed", 409)
            status = (
                BotConfigApplicationStatus.APPLIED if valid else BotConfigApplicationStatus.REJECTED
            )
            uow.configs.settle_application(
                current.application_id,
                current.row_version,
                status,
                "platform-gateway-probe",
                None if valid else "gateway_probe_failed",
                None if valid else "Gateway connection or login unavailable",
                uuid4(),
                now,
            )
            if valid:
                uow.services.set_applied_if_desired(service_id, current.config_version_id, now)
            uow.audit.add(
                NewAuditEntry(
                    audit_id=uuid4(),
                    actor_type="administrator",
                    actor_id=None,
                    action="bot.gateway.probed",
                    target_type="bot_service",
                    target_id=service_id,
                    correlation_id=current.application_id,
                    details={
                        "applied": bool(valid),
                        "config_version_id": str(current.config_version_id),
                    },
                )
            )
            uow.commit()
        return {"applied": bool(valid), "status": status.value}
