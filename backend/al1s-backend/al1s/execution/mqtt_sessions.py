from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import TracebackType
from typing import Protocol
from uuid import UUID, uuid4

from al1s.execution.errors import ConflictError, ExecutionDomainError


class MqttSessionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class TerminalMqttSessionRecord:
    session_id: UUID
    terminal_id: UUID
    client_id: str
    username: str
    role_name: str
    topic: str
    password_digest: str
    status: MqttSessionStatus
    issued_at: datetime
    expires_at: datetime
    configured_at: datetime | None
    revoked_at: datetime | None
    row_version: int


@dataclass(frozen=True, slots=True)
class TerminalMqttSessionGrant:
    session_id: UUID
    broker_host: str
    broker_port: int
    tls_enabled: bool
    client_id: str
    username: str
    password: str
    topic: str
    qos: int
    issued_at: datetime
    expires_at: datetime


class MqttSessionPasswordSigner:
    def __init__(self, signing_key: str) -> None:
        if len(signing_key.encode("utf-8")) < 32:
            raise ValueError("MQTT session signing key must contain at least 32 UTF-8 bytes")
        self._key = signing_key.encode("utf-8")

    def password(self, session: TerminalMqttSessionRecord) -> str:
        canonical = "\n".join(
            (
                "al1s-terminal-mqtt-v1",
                str(session.session_id),
                str(session.terminal_id),
                session.client_id,
                session.username,
                session.role_name,
                session.topic,
                session.issued_at.isoformat(),
                session.expires_at.isoformat(),
            )
        ).encode("utf-8")
        digest = hmac.new(self._key, canonical, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    @staticmethod
    def digest(password: str) -> str:
        return hashlib.sha256(password.encode("utf-8")).hexdigest()


class TerminalMqttSessionRepository(Protocol):
    def lock_terminal(self, terminal_id: UUID) -> None: ...

    def get_reusable(
        self, terminal_id: UUID, *, valid_after: datetime
    ) -> TerminalMqttSessionRecord | None: ...

    def add(self, session: TerminalMqttSessionRecord) -> None: ...

    def get_by_id(self, session_id: UUID) -> TerminalMqttSessionRecord | None: ...

    def mark_active(
        self, session_id: UUID, expected_version: int, *, configured_at: datetime
    ) -> TerminalMqttSessionRecord | None: ...

    def list_expired(self, *, now: datetime, limit: int) -> Sequence[TerminalMqttSessionRecord]: ...

    def mark_expired(
        self, session_id: UUID, expected_version: int, *, expired_at: datetime
    ) -> bool: ...


class TerminalMqttSessionUnitOfWork(Protocol):
    sessions: TerminalMqttSessionRepository

    def __enter__(self) -> TerminalMqttSessionUnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...


class MqttSecurityBroker(Protocol):
    def ensure_terminal_subscriber(
        self, session: TerminalMqttSessionRecord, password: str
    ) -> None: ...

    def revoke_terminal_subscriber(self, session: TerminalMqttSessionRecord) -> None: ...


class MqttProvisioningError(ExecutionDomainError):
    def __init__(self) -> None:
        super().__init__(
            "mqtt_session_provisioning_failed",
            "MQTT session could not be provisioned",
            503,
        )


class TerminalMqttSessionService:
    def __init__(
        self,
        uow_factory: Callable[[], TerminalMqttSessionUnitOfWork],
        broker: MqttSecurityBroker,
        signer: MqttSessionPasswordSigner,
        *,
        public_host: str,
        public_port: int,
        tls_enabled: bool,
        ttl: timedelta = timedelta(hours=1),
        renew_before: timedelta = timedelta(minutes=5),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not public_host.strip():
            raise ValueError("MQTT public host is required")
        if not 1 <= public_port <= 65535:
            raise ValueError("MQTT public port is invalid")
        if ttl < timedelta(minutes=5) or ttl > timedelta(hours=24):
            raise ValueError("MQTT session TTL must be between 5 minutes and 24 hours")
        if renew_before < timedelta(minutes=1) or renew_before >= ttl:
            raise ValueError("MQTT renewal window must be at least 1 minute and less than TTL")
        self._uow_factory = uow_factory
        self._broker = broker
        self._signer = signer
        self._public_host = public_host.strip()
        self._public_port = public_port
        self._tls_enabled = tls_enabled
        self._ttl = ttl
        self._renew_before = renew_before
        self._now = now or (lambda: datetime.now(UTC))

    def issue(self, terminal_id: UUID) -> TerminalMqttSessionGrant:
        now = self._now()
        with self._uow_factory() as uow:
            uow.sessions.lock_terminal(terminal_id)
            session = uow.sessions.get_reusable(
                terminal_id,
                valid_after=now + self._renew_before,
            )
            if session is None:
                session = self._new_session(terminal_id, now)
                uow.sessions.add(session)
            uow.commit()

        password = self._signer.password(session)
        if not hmac.compare_digest(session.password_digest, self._signer.digest(password)):
            raise ConflictError("mqtt_session_digest_mismatch", "MQTT session digest is invalid")
        try:
            self._broker.ensure_terminal_subscriber(session, password)
        except Exception as exc:
            raise MqttProvisioningError() from exc

        if session.status is MqttSessionStatus.PENDING:
            with self._uow_factory() as uow:
                active = uow.sessions.mark_active(
                    session.session_id,
                    session.row_version,
                    configured_at=self._now(),
                )
                if active is None:
                    active = uow.sessions.get_by_id(session.session_id)
                if active is None or active.status is not MqttSessionStatus.ACTIVE:
                    raise ConflictError("mqtt_session_changed", "MQTT session changed concurrently")
                uow.commit()
            session = active
        return self._grant(session, password)

    def revoke_expired(self, *, limit: int = 50) -> int:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        now = self._now()
        with self._uow_factory() as uow:
            candidates = tuple(uow.sessions.list_expired(now=now, limit=limit))
        revoked = 0
        for candidate in candidates:
            try:
                self._broker.revoke_terminal_subscriber(candidate)
            except Exception:
                continue
            with self._uow_factory() as uow:
                if uow.sessions.mark_expired(
                    candidate.session_id,
                    candidate.row_version,
                    expired_at=now,
                ):
                    uow.commit()
                    revoked += 1
        return revoked

    def _new_session(self, terminal_id: UUID, now: datetime) -> TerminalMqttSessionRecord:
        session_id = uuid4()
        short_id = session_id.hex
        record = TerminalMqttSessionRecord(
            session_id=session_id,
            terminal_id=terminal_id,
            client_id=f"al1s-terminal-{short_id}",
            username=f"terminal-{short_id}",
            role_name=f"terminal-hints-{short_id}",
            topic=f"al1s/v1/terminals/{terminal_id}/hints",
            password_digest="",
            status=MqttSessionStatus.PENDING,
            issued_at=now,
            expires_at=now + self._ttl,
            configured_at=None,
            revoked_at=None,
            row_version=1,
        )
        password = self._signer.password(record)
        return replace(record, password_digest=self._signer.digest(password))

    def _grant(
        self, session: TerminalMqttSessionRecord, password: str
    ) -> TerminalMqttSessionGrant:
        return TerminalMqttSessionGrant(
            session_id=session.session_id,
            broker_host=self._public_host,
            broker_port=self._public_port,
            tls_enabled=self._tls_enabled,
            client_id=session.client_id,
            username=session.username,
            password=password,
            topic=session.topic,
            qos=1,
            issued_at=session.issued_at,
            expires_at=session.expires_at,
        )
