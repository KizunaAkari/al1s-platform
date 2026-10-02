from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from al1s.bots.errors import BotAuthenticationError, BotDomainError
from al1s.bots.ports import BotUnitOfWork
from al1s.bots.types import (
    BotConfigApplicationRecord,
    BotConfigApplicationStatus,
    BotConfigVersionRecord,
    BotHealthReportRecord,
    BotHealthStatus,
    BotHistoryCleanupResult,
    BotRegistrationGrantRecord,
    BotServiceKind,
    BotServiceRecord,
    BotWorkerConfig,
    BotWorkerIdentityRecord,
    IssuedBotRegistrationGrant,
    RegisteredBotWorker,
    SubmittedBotConfig,
)
from al1s.kernel.types import NewAuditEntry, NewOutboxEvent
from al1s.secrets.digests import ParsedSecret, SecretDigester
from al1s.secrets.ports import SecretCipher, SecretFingerprinter
from al1s.secrets.types import SecretRecord

BotUowFactory = Callable[[], BotUnitOfWork]


class BotService:
    def __init__(
        self,
        uow_factory: BotUowFactory,
        cipher: SecretCipher,
        fingerprinter: SecretFingerprinter,
        *,
        digester: SecretDigester | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._cipher = cipher
        self._fingerprinter = fingerprinter
        self._digester = digester or SecretDigester()
        self._now = now or (lambda: datetime.now(UTC))

    def create_service(
        self,
        *,
        service_id: UUID,
        kind: BotServiceKind,
        name: str,
        correlation_id: UUID,
    ) -> BotServiceRecord:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 100:
            raise BotDomainError("invalid_bot_service_name", "Bot service name is invalid")
        now = self._now()
        record = BotServiceRecord(
            service_id=service_id,
            kind=kind,
            name=normalized_name,
            enabled=True,
            desired_config_version_id=None,
            applied_config_version_id=None,
            row_version=1,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        with self._uow_factory() as uow:
            if not uow.services.add(record):
                existing = uow.services.get(service_id)
                if (
                    existing is not None
                    and existing.kind == kind
                    and existing.name == normalized_name
                ):
                    return existing
                raise BotDomainError(
                    "bot_service_conflict", "Bot service or name already exists", 409
                )
            _record_event(
                uow,
                event_type="bot.service.created.v1",
                aggregate_type="bot_service",
                aggregate_id=service_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={"service_id": str(service_id), "kind": kind.value},
            )
            uow.commit()
        return record

    def list_services(self, *, after_id: UUID | None, limit: int) -> list[BotServiceRecord]:
        with self._uow_factory() as uow:
            return uow.services.list_active(after_id=after_id, limit=limit)

    def get_service(self, service_id: UUID) -> BotServiceRecord:
        with self._uow_factory() as uow:
            return _require_service(uow, service_id)

    def get_config_version(
        self, service_id: UUID, config_version_id: UUID
    ) -> BotConfigVersionRecord:
        with self._uow_factory() as uow:
            _require_service(uow, service_id)
            version = uow.configs.get_version(config_version_id)
            if version is None or version.service_id != service_id:
                raise BotDomainError("bot_config_not_found", "Bot config version not found", 404)
            return version

    def list_config_applications(
        self,
        service_id: UUID,
        *,
        before_requested_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[BotConfigApplicationRecord]:
        with self._uow_factory() as uow:
            _require_service(uow, service_id)
            return uow.configs.list_applications(
                service_id,
                before_requested_at=before_requested_at,
                before_id=before_id,
                limit=limit,
            )

    def list_health_reports(
        self,
        service_id: UUID,
        *,
        before_reported_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> list[BotHealthReportRecord]:
        with self._uow_factory() as uow:
            _require_service(uow, service_id)
            return uow.health.list_for_service(
                service_id,
                before_reported_at=before_reported_at,
                before_id=before_id,
                limit=limit,
            )

    def cleanup_history(
        self, *, cutoff: datetime, limit: int, correlation_id: UUID
    ) -> BotHistoryCleanupResult:
        if cutoff.tzinfo is None:
            raise BotDomainError(
                "invalid_bot_history_cutoff", "History cutoff must be timezone-aware"
            )
        now = self._now()
        with self._uow_factory() as uow:
            deleted_health = uow.health.delete_before(cutoff, limit=limit)
            deleted_applications = uow.configs.delete_settled_applications_before(
                cutoff, limit=limit
            )
            secret_ids = uow.configs.delete_unreferenced_versions_before(cutoff, limit=limit)
            deleted_secrets = uow.secrets.delete_many_if_unreferenced(
                [secret_id for secret_id in secret_ids if secret_id is not None]
            )
            result = BotHistoryCleanupResult(
                deleted_health_reports=deleted_health,
                deleted_applications=deleted_applications,
                deleted_config_versions=len(secret_ids),
                deleted_secrets=deleted_secrets,
            )
            _record_event(
                uow,
                event_type="bot.history.cleaned.v1",
                aggregate_type="bot_history",
                aggregate_id=UUID(int=0),
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "cutoff": cutoff.isoformat(),
                    "limit_per_table": limit,
                    "deleted_health_reports": result.deleted_health_reports,
                    "deleted_applications": result.deleted_applications,
                    "deleted_config_versions": result.deleted_config_versions,
                    "deleted_encrypted_records": result.deleted_secrets,
                },
            )
            uow.commit()
        return result

    def create_registration_grant(
        self,
        *,
        service_id: UUID,
        ttl: timedelta,
        correlation_id: UUID,
    ) -> IssuedBotRegistrationGrant:
        if not timedelta(minutes=1) <= ttl <= timedelta(hours=24):
            raise BotDomainError(
                "invalid_bot_registration_grant_ttl",
                "Registration grant TTL must be between 1 minute and 24 hours",
            )
        now = self._now()
        grant_id = uuid4()
        registration_code, digest = self._digester.issue(grant_id)
        expires_at = now + ttl
        with self._uow_factory() as uow:
            service = _require_service(uow, service_id)
            if not service.enabled:
                raise BotDomainError("bot_service_disabled", "Bot service is disabled", 409)
            uow.grants.add(
                BotRegistrationGrantRecord(
                    grant_id=grant_id,
                    service_id=service_id,
                    secret_digest=digest,
                    expires_at=expires_at,
                    consumed_at=None,
                    consumed_by_identity_id=None,
                    row_version=1,
                )
            )
            _record_event(
                uow,
                event_type="bot.registration_grant.created.v1",
                aggregate_type="bot_registration_grant",
                aggregate_id=grant_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={"grant_id": str(grant_id), "service_id": str(service_id)},
            )
            uow.commit()
        return IssuedBotRegistrationGrant(grant_id, registration_code, expires_at)

    def register_worker(
        self, *, registration_code: str, correlation_id: UUID
    ) -> RegisteredBotWorker:
        parsed = _parse_secret(registration_code, registration=True)
        now = self._now()
        try:
            with self._uow_factory() as uow:
                grant = uow.grants.get(parsed.identifier, for_update=True)
                if grant is None or not self._digester.verify(parsed.secret, grant.secret_digest):
                    raise BotDomainError(
                        "invalid_bot_registration_code", "Registration code is invalid", 401
                    )
                if grant.consumed_at is not None:
                    raise BotDomainError(
                        "bot_registration_code_consumed",
                        "Registration code has already been used",
                        409,
                    )
                if grant.expires_at <= now:
                    raise BotDomainError(
                        "bot_registration_code_expired", "Registration code has expired", 409
                    )
                service = _require_service(uow, grant.service_id)
                if not service.enabled:
                    raise BotDomainError("bot_service_disabled", "Bot service is disabled", 409)
                if uow.identities.get_for_service(service.service_id, for_update=True) is not None:
                    raise BotDomainError(
                        "bot_worker_already_registered",
                        "This Bot service already has a worker identity",
                        409,
                    )
                identity_id = uuid4()
                credential, digest = self._digester.issue(identity_id)
                identity = BotWorkerIdentityRecord(
                    identity_id=identity_id,
                    service_id=service.service_id,
                    credential_digest=digest,
                    credential_version=1,
                    enabled=True,
                    created_at=now,
                    rotated_at=None,
                    last_seen_at=None,
                )
                uow.identities.add(identity)
                if not uow.grants.consume(grant.grant_id, grant.row_version, identity_id, now):
                    raise BotDomainError(
                        "bot_registration_code_consumed",
                        "Registration code has already been used",
                        409,
                    )
                _record_event(
                    uow,
                    event_type="bot.worker.registered.v1",
                    aggregate_type="bot_service",
                    aggregate_id=service.service_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                    payload={
                        "service_id": str(service.service_id),
                        "identity_id": str(identity_id),
                    },
                )
                uow.commit()
        except IntegrityError as exc:
            raise BotDomainError(
                "bot_worker_registration_conflict", "Bot worker registration conflicted", 409
            ) from exc
        return RegisteredBotWorker(identity_id, service.service_id, credential)

    def submit_config(
        self,
        *,
        service_id: UUID,
        config_version_id: UUID,
        application_id: UUID,
        expected_version: int,
        settings: dict[str, Any],
        secret: str | None,
        onebot_service_id: UUID | None,
        correlation_id: UUID,
        retain_secret_from_config_version_id: UUID | None = None,
    ) -> SubmittedBotConfig:
        if retain_secret_from_config_version_id is not None and secret is not None:
            raise BotDomainError("bot_secret_source_conflict", "Supply a new secret or retain one")
        normalized_settings = _validate_settings(settings)
        now = self._now()
        with self._uow_factory() as uow:
            service = _require_service(uow, service_id, for_update=True)
            retained = None
            if retain_secret_from_config_version_id is not None:
                source = uow.configs.get_version(retain_secret_from_config_version_id)
                if source is None or source.service_id != service_id or source.secret_id is None:
                    raise BotDomainError(
                        "bot_secret_source_invalid", "Secret source is unavailable", 409
                    )
                retained = uow.secrets.get(source.secret_id)
                if retained is None or retained.purpose != f"bot_service:{service_id}":
                    raise BotDomainError(
                        "bot_secret_source_invalid", "Secret source is unavailable", 409
                    )
            existing = uow.configs.get_version(config_version_id)
            if existing is not None:
                application = uow.configs.get_application(application_id)
                if (
                    application is None
                    or application.config_version_id != existing.config_version_id
                ):
                    raise BotDomainError(
                        "idempotency_key_conflict",
                        "Idempotency key was already used with another request",
                        409,
                    )
                _verify_replayed_config(
                    uow, existing, normalized_settings, secret, onebot_service_id,
                    self._cipher, retained_secret_id=retained.secret_id if retained else None,
                )
                return SubmittedBotConfig(service, existing, application)
            _check_version(service, expected_version)
            if (
                retain_secret_from_config_version_id is not None
                and service.desired_config_version_id != retain_secret_from_config_version_id
            ):
                raise BotDomainError(
                    "stale_bot_config_source", "Bot config changed concurrently", 409
                )
            if not service.enabled:
                raise BotDomainError("bot_service_disabled", "Bot service is disabled", 409)
            _validate_relation(uow, service, onebot_service_id)
            secret_record = _new_secret(self._cipher, service_id, secret, now)
            effective_secret = (
                self._cipher.decrypt(retained.ciphertext, retained.key_id)
                if retained is not None else secret
            )
            fingerprint = (
                self._fingerprinter.fingerprint(effective_secret) if effective_secret else None
            )
            config_hash = _config_hash(normalized_settings, onebot_service_id, fingerprint)
            version = BotConfigVersionRecord(
                config_version_id=config_version_id,
                service_id=service_id,
                version_no=uow.configs.next_version_no(service_id),
                settings=normalized_settings,
                secret_id=(
                    retained.secret_id if retained else
                    secret_record.secret_id if secret_record else None
                ),
                onebot_service_id=onebot_service_id,
                config_hash=config_hash,
                created_at=now,
            )
            application = BotConfigApplicationRecord(
                application_id=application_id,
                service_id=service_id,
                config_version_id=config_version_id,
                status=BotConfigApplicationStatus.PENDING,
                worker_instance_id=None,
                error_code=None,
                error_summary=None,
                receipt_event_id=None,
                requested_at=now,
                completed_at=None,
                row_version=1,
            )
            if secret_record is not None:
                uow.secrets.add(secret_record)
                # Secret and Bot config are separate aggregate tables without an
                # ORM relationship. Flush the encrypted parent before its FK child.
                uow.flush()
            uow.configs.add_version(version)
            updated = uow.services.set_desired(
                service_id, service.row_version, config_version_id, now
            )
            if updated is None:
                raise BotDomainError("stale_bot_service", "Bot service changed concurrently", 409)
            # The desired pointer update triggers an autoflush of the immutable
            # version. Add the dependent application afterwards so SQLAlchemy
            # cannot insert it before its foreign-key parent in this cyclic graph.
            uow.configs.add_application(application)
            _record_event(
                uow,
                event_type="bot.config.requested.v1",
                aggregate_type="bot_service",
                aggregate_id=service_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "service_id": str(service_id),
                    "config_version_id": str(config_version_id),
                    "application_id": str(application_id),
                    "version_no": version.version_no,
                    "config_hash": config_hash,
                },
            )
            uow.commit()
        return SubmittedBotConfig(updated, version, application)

    def get_worker_config(self, credential: str) -> BotWorkerConfig | None:
        now = self._now()
        with self._uow_factory() as uow:
            identity, service = self._authenticate(uow, credential)
            application = uow.configs.get_pending_for_service(service.service_id)
            if application is None:
                uow.identities.touch(identity.identity_id, now)
                uow.commit()
                return None
            version = uow.configs.get_version(application.config_version_id)
            if version is None:
                raise BotDomainError("bot_config_missing", "Desired Bot config is unavailable", 500)
            secret_record = uow.secrets.get(version.secret_id) if version.secret_id else None
            secret = (
                self._cipher.decrypt(secret_record.ciphertext, secret_record.key_id)
                if secret_record is not None
                else None
            )
            uow.identities.touch(identity.identity_id, now)
            uow.commit()
        return BotWorkerConfig(application, version, secret, None)

    def settle_config_application(
        self,
        *,
        credential: str,
        application_id: UUID,
        receipt_event_id: UUID,
        status: BotConfigApplicationStatus,
        worker_instance_id: str,
        error_code: str | None,
        error_summary: str | None,
        correlation_id: UUID,
    ) -> BotConfigApplicationRecord:
        if status is BotConfigApplicationStatus.PENDING:
            raise BotDomainError("invalid_bot_application_result", "Result must be terminal")
        normalized_worker = worker_instance_id.strip()
        if not normalized_worker or len(normalized_worker) > 255:
            raise BotDomainError("invalid_bot_worker_instance", "Worker instance is invalid")
        clean_error_code, clean_summary = _validate_result_error(status, error_code, error_summary)
        now = self._now()
        with self._uow_factory() as uow:
            identity, _ = self._authenticate(uow, credential)
            replay = uow.configs.get_application_by_receipt(receipt_event_id)
            if replay is not None:
                if replay.application_id != application_id:
                    raise BotDomainError(
                        "bot_receipt_event_conflict",
                        "Receipt event was used for another result",
                        409,
                    )
                return replay
            current = uow.configs.get_application(application_id, for_update=True)
            if current is None or current.service_id != identity.service_id:
                raise BotDomainError(
                    "bot_config_application_not_found", "Application not found", 404
                )
            if current.status is not BotConfigApplicationStatus.PENDING:
                if current.receipt_event_id == receipt_event_id:
                    return current
                raise BotDomainError(
                    "bot_config_application_already_settled",
                    "Application already has a terminal result",
                    409,
                )
            settled = uow.configs.settle_application(
                application_id,
                current.row_version,
                status,
                normalized_worker,
                clean_error_code,
                clean_summary,
                receipt_event_id,
                now,
            )
            if settled is None:
                raise BotDomainError(
                    "stale_bot_config_application", "Application changed concurrently", 409
                )
            if status is BotConfigApplicationStatus.APPLIED:
                uow.services.set_applied_if_desired(
                    current.service_id, current.config_version_id, now
                )
            uow.identities.touch(identity.identity_id, now)
            _record_event(
                uow,
                event_type=f"bot.config.{status.value}.v1",
                aggregate_type="bot_config_application",
                aggregate_id=application_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "service_id": str(current.service_id),
                    "config_version_id": str(current.config_version_id),
                    "application_id": str(application_id),
                    "status": status.value,
                    "error_code": clean_error_code,
                },
            )
            uow.commit()
        return settled

    def heartbeat(
        self,
        *,
        credential: str,
        receipt_event_id: UUID,
        config_version_id: UUID | None,
        status: BotHealthStatus,
        diagnostics: dict[str, Any],
    ) -> BotHealthReportRecord:
        clean_diagnostics = _validate_diagnostics(diagnostics)
        now = self._now()
        with self._uow_factory() as uow:
            identity, service = self._authenticate(uow, credential)
            replay = uow.health.get_by_receipt(receipt_event_id)
            if replay is not None:
                if replay.service_id != service.service_id:
                    raise BotDomainError(
                        "bot_health_receipt_conflict",
                        "Health receipt belongs to another service",
                        409,
                    )
                return replay
            if config_version_id is not None:
                version = uow.configs.get_version(config_version_id)
                if version is None or version.service_id != service.service_id:
                    raise BotDomainError(
                        "bot_health_config_mismatch", "Health config does not belong to service"
                    )
            report_id = uuid4()
            inserted = uow.health.add(
                report_id=report_id,
                receipt_event_id=receipt_event_id,
                service_id=service.service_id,
                identity_id=identity.identity_id,
                config_version_id=config_version_id,
                status=status,
                diagnostics=clean_diagnostics,
                reported_at=now,
            )
            if not inserted:
                replay = uow.health.get_by_receipt(receipt_event_id)
                if replay is None:
                    raise BotDomainError(
                        "bot_health_receipt_conflict",
                        "Health receipt could not be resolved",
                        409,
                    )
                if replay.service_id != service.service_id:
                    raise BotDomainError(
                        "bot_health_receipt_conflict",
                        "Health receipt belongs to another service",
                        409,
                    )
                return replay
            uow.identities.touch(identity.identity_id, now)
            uow.commit()
        return BotHealthReportRecord(
            report_id,
            receipt_event_id,
            service.service_id,
            identity.identity_id,
            config_version_id,
            status,
            clean_diagnostics,
            now,
        )

    def rotate_credential(
        self, *, service_id: UUID, expected_version: int, correlation_id: UUID
    ) -> RegisteredBotWorker:
        now = self._now()
        with self._uow_factory() as uow:
            service = _require_service(uow, service_id, for_update=True)
            _check_version(service, expected_version)
            identity = uow.identities.get_for_service(service_id, for_update=True)
            if identity is None:
                raise BotDomainError(
                    "bot_worker_not_registered", "Bot worker is not registered", 409
                )
            credential, digest = self._digester.issue(identity.identity_id)
            if not uow.identities.replace_credential(
                identity.identity_id, digest, identity.credential_version + 1, now
            ):
                raise BotDomainError(
                    "bot_worker_not_registered", "Bot worker is not registered", 409
                )
            updated = uow.services.bump_version(service_id, service.row_version, now)
            if updated is None:
                raise BotDomainError("stale_bot_service", "Bot service changed concurrently", 409)
            _record_event(
                uow,
                event_type="bot.worker.credential_rotated.v1",
                aggregate_type="bot_service",
                aggregate_id=service_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={
                    "service_id": str(service_id),
                    "credential_version": identity.credential_version + 1,
                },
            )
            uow.commit()
        return RegisteredBotWorker(identity.identity_id, service_id, credential)

    def disable_service(
        self, *, service_id: UUID, expected_version: int, correlation_id: UUID
    ) -> BotServiceRecord:
        now = self._now()
        with self._uow_factory() as uow:
            service = _require_service(uow, service_id, for_update=True)
            _check_version(service, expected_version)
            updated = uow.services.disable(service_id, service.row_version, now)
            if updated is None:
                raise BotDomainError("stale_bot_service", "Bot service changed concurrently", 409)
            uow.identities.disable_for_service(service_id, now)
            _record_event(
                uow,
                event_type="bot.service.disabled.v1",
                aggregate_type="bot_service",
                aggregate_id=service_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload={"service_id": str(service_id)},
            )
            uow.commit()
        return updated

    def worker_service_id(self, credential: str) -> UUID:
        with self._uow_factory() as uow:
            _, service = self._authenticate(uow, credential)
            if service.kind != BotServiceKind.DISCORD_BRIDGE:
                raise BotAuthenticationError()
            return service.service_id

    def _authenticate(
        self, uow: BotUnitOfWork, credential: str
    ) -> tuple[BotWorkerIdentityRecord, BotServiceRecord]:
        parsed = _parse_secret(credential, registration=False)
        identity = uow.identities.get(parsed.identifier)
        if (
            identity is None
            or not identity.enabled
            or not self._digester.verify(parsed.secret, identity.credential_digest)
        ):
            raise BotAuthenticationError()
        service = uow.services.get(identity.service_id)
        if service is None or not service.enabled or service.deleted_at is not None:
            raise BotAuthenticationError()
        return identity, service


def _parse_secret(value: str, *, registration: bool) -> ParsedSecret:
    identifier_text, separator, secret = value.partition(".")
    if not separator or not secret:
        if registration:
            raise BotDomainError(
                "invalid_bot_registration_code", "Registration code is invalid", 401
            )
        raise BotAuthenticationError()
    try:
        return ParsedSecret(UUID(identifier_text), secret)
    except ValueError as exc:
        if registration:
            raise BotDomainError(
                "invalid_bot_registration_code", "Registration code is invalid", 401
            ) from exc
        raise BotAuthenticationError() from exc


def _require_service(
    uow: BotUnitOfWork, service_id: UUID, *, for_update: bool = False
) -> BotServiceRecord:
    service = uow.services.get(service_id, for_update=for_update)
    if service is None or service.deleted_at is not None:
        raise BotDomainError("bot_service_not_found", "Bot service not found", 404)
    return service


def _check_version(service: BotServiceRecord, expected_version: int) -> None:
    if expected_version < 1 or service.row_version != expected_version:
        raise BotDomainError("stale_bot_service", "Bot service changed concurrently", 409)


def _validate_relation(
    uow: BotUnitOfWork, service: BotServiceRecord, onebot_service_id: UUID | None
) -> None:
    if service.kind is BotServiceKind.ONEBOT_GATEWAY:
        if onebot_service_id is not None:
            raise BotDomainError(
                "onebot_config_cannot_reference_gateway",
                "OneBot gateway config cannot reference another gateway",
            )
        return
    if onebot_service_id is None:
        return
    target = _require_service(uow, onebot_service_id)
    if target.kind is not BotServiceKind.ONEBOT_GATEWAY or not target.enabled:
        raise BotDomainError(
            "invalid_onebot_service", "Referenced service is not an enabled OneBot gateway"
        )


def _validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(settings, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise BotDomainError("invalid_bot_settings", "Bot settings must be valid JSON") from exc
    if len(encoded.encode("utf-8")) > 32_768:
        raise BotDomainError("bot_settings_too_large", "Bot settings exceed 32 KiB")
    normalized = json.loads(encoded)
    if _contains_sensitive_setting_key(normalized):
        raise BotDomainError(
            "bot_secret_in_settings",
            "Secrets must be supplied through the dedicated secret field",
        )
    return normalized  # type: ignore[no-any-return]


def _contains_sensitive_setting_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            folded_key = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if any(marker in folded_key for marker in _SENSITIVE_SETTING_KEY_MARKERS):
                return True
            if _contains_sensitive_setting_key(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_sensitive_setting_key(item) for item in value)
    return False


_SENSITIVE_SETTING_KEY_MARKERS = (
    "apikey",
    "accesskey",
    "authorization",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
)


def _validate_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
    result = _validate_settings(diagnostics)
    if len(result) > 50:
        raise BotDomainError("bot_diagnostics_too_large", "Bot diagnostics has too many fields")
    return result


def _new_secret(
    cipher: SecretCipher, service_id: UUID, secret: str | None, now: datetime
) -> SecretRecord | None:
    if secret is None:
        return None
    if not secret or len(secret) > 4_096:
        raise BotDomainError("invalid_bot_secret", "Bot secret is invalid")
    return SecretRecord(
        secret_id=uuid4(),
        purpose=f"bot_service:{service_id}",
        ciphertext=cipher.encrypt(secret),
        key_id=cipher.key_id,
        row_version=1,
        created_at=now,
        updated_at=now,
    )


def _config_hash(
    settings: dict[str, Any], onebot_service_id: UUID | None, secret_fingerprint: str | None
) -> str:
    canonical = json.dumps(
        {
            "settings": settings,
            "onebot_service_id": str(onebot_service_id) if onebot_service_id else None,
            "secret_fingerprint": secret_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verify_replayed_config(
    uow: BotUnitOfWork,
    existing: BotConfigVersionRecord,
    settings: dict[str, Any],
    secret: str | None,
    onebot_service_id: UUID | None,
    cipher: SecretCipher,
    *,
    retained_secret_id: UUID | None = None,
) -> None:
    stored_secret = uow.secrets.get(existing.secret_id) if existing.secret_id else None
    plaintext = (
        cipher.decrypt(stored_secret.ciphertext, stored_secret.key_id)
        if stored_secret is not None
        else None
    )
    if (
        existing.settings != settings
        or existing.onebot_service_id != onebot_service_id
        or (existing.secret_id != retained_secret_id if retained_secret_id is not None
            else plaintext != secret)
    ):
        raise BotDomainError(
            "idempotency_key_conflict",
            "Idempotency key was already used with another request",
            409,
        )


def _validate_result_error(
    status: BotConfigApplicationStatus, error_code: str | None, error_summary: str | None
) -> tuple[str | None, str | None]:
    if status is BotConfigApplicationStatus.APPLIED:
        return None, None
    code = (error_code or "").strip()
    summary = (error_summary or "").strip()
    if not code or len(code) > 100 or not summary or len(summary) > 500:
        raise BotDomainError(
            "invalid_bot_rejection_reason", "Rejected config requires a bounded error reason"
        )
    return code, summary


def _record_event(
    uow: BotUnitOfWork,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    correlation_id: UUID,
    occurred_at: datetime,
    payload: dict[str, Any],
) -> None:
    uow.audit.add(
        NewAuditEntry(
            audit_id=uuid4(),
            actor_type="system",
            actor_id=None,
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
