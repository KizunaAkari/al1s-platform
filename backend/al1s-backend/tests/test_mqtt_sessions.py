from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from uuid import UUID, uuid4

from al1s.execution.mqtt_sessions import (
    MqttSessionPasswordSigner,
    MqttSessionStatus,
    TerminalMqttSessionRecord,
    TerminalMqttSessionService,
)


class MemorySessionRepository:
    def __init__(self) -> None:
        self.records: dict[UUID, TerminalMqttSessionRecord] = {}

    def lock_terminal(self, terminal_id: UUID) -> None:
        del terminal_id

    def get_reusable(
        self, terminal_id: UUID, *, valid_after: datetime
    ) -> TerminalMqttSessionRecord | None:
        candidates = [
            record
            for record in self.records.values()
            if record.terminal_id == terminal_id
            and record.status in {MqttSessionStatus.PENDING, MqttSessionStatus.ACTIVE}
            and record.expires_at > valid_after
        ]
        return max(candidates, key=lambda item: item.issued_at) if candidates else None

    def add(self, session: TerminalMqttSessionRecord) -> None:
        self.records[session.session_id] = session

    def get_by_id(self, session_id: UUID) -> TerminalMqttSessionRecord | None:
        return self.records.get(session_id)

    def mark_active(
        self, session_id: UUID, expected_version: int, *, configured_at: datetime
    ) -> TerminalMqttSessionRecord | None:
        current = self.records[session_id]
        if (
            current.row_version != expected_version
            or current.status is not MqttSessionStatus.PENDING
        ):
            return None
        updated = replace(
            current,
            status=MqttSessionStatus.ACTIVE,
            configured_at=configured_at,
            row_version=current.row_version + 1,
        )
        self.records[session_id] = updated
        return updated

    def list_expired(
        self, *, now: datetime, limit: int
    ) -> Sequence[TerminalMqttSessionRecord]:
        return tuple(
            sorted(
                (
                    record
                    for record in self.records.values()
                    if record.status in {MqttSessionStatus.PENDING, MqttSessionStatus.ACTIVE}
                    and record.expires_at <= now
                ),
                key=lambda item: (item.expires_at, item.session_id),
            )[:limit]
        )

    def mark_expired(
        self, session_id: UUID, expected_version: int, *, expired_at: datetime
    ) -> bool:
        current = self.records[session_id]
        if (
            current.row_version != expected_version
            or current.status not in {MqttSessionStatus.PENDING, MqttSessionStatus.ACTIVE}
        ):
            return False
        self.records[session_id] = replace(
            current,
            status=MqttSessionStatus.EXPIRED,
            revoked_at=expired_at,
            row_version=current.row_version + 1,
        )
        return True


class MemoryUnitOfWork:
    def __init__(self, sessions: MemorySessionRepository) -> None:
        self.sessions = sessions
        self.commits = 0

    def __enter__(self) -> MemoryUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


class FakeBroker:
    def __init__(self) -> None:
        self.provisioned: list[tuple[TerminalMqttSessionRecord, str]] = []
        self.revoked: list[TerminalMqttSessionRecord] = []

    def ensure_terminal_subscriber(
        self, session: TerminalMqttSessionRecord, password: str
    ) -> None:
        self.provisioned.append((session, password))

    def revoke_terminal_subscriber(self, session: TerminalMqttSessionRecord) -> None:
        self.revoked.append(session)


class ConcurrentActivationRepository(MemorySessionRepository):
    def mark_active(
        self, session_id: UUID, expected_version: int, *, configured_at: datetime
    ) -> TerminalMqttSessionRecord | None:
        activated = super().mark_active(
            session_id,
            expected_version,
            configured_at=configured_at,
        )
        assert activated is not None
        return None


def test_issue_reuses_a_short_lived_session_without_storing_plaintext_password() -> None:
    now = datetime(2026, 9, 1, 8, tzinfo=UTC)
    terminal_id = uuid4()
    repository = MemorySessionRepository()
    broker = FakeBroker()
    service = TerminalMqttSessionService(
        lambda: MemoryUnitOfWork(repository),
        broker,
        MqttSessionPasswordSigner("test-mqtt-session-signing-key-at-least-32-bytes"),
        public_host="mqtt.example.test",
        public_port=8883,
        tls_enabled=True,
        now=lambda: now,
    )

    first = service.issue(terminal_id)
    second = service.issue(terminal_id)

    assert first == second
    assert first.password
    assert first.topic == f"al1s/v1/terminals/{terminal_id}/hints"
    stored = repository.records[first.session_id]
    assert stored.status is MqttSessionStatus.ACTIVE
    assert stored.password_digest != first.password
    assert len(stored.password_digest) == 64
    assert [item[0].session_id for item in broker.provisioned] == [
        first.session_id,
        first.session_id,
    ]


def test_expired_session_is_revoked_before_database_state_is_finalized() -> None:
    clock = [datetime(2026, 9, 1, 8, tzinfo=UTC)]
    terminal_id = uuid4()
    repository = MemorySessionRepository()
    broker = FakeBroker()
    service = TerminalMqttSessionService(
        lambda: MemoryUnitOfWork(repository),
        broker,
        MqttSessionPasswordSigner("test-mqtt-session-signing-key-at-least-32-bytes"),
        public_host="mqtt.example.test",
        public_port=1883,
        tls_enabled=False,
        ttl=timedelta(minutes=5),
        renew_before=timedelta(minutes=1),
        now=lambda: clock[0],
    )
    grant = service.issue(terminal_id)
    clock[0] += timedelta(minutes=6)

    assert service.revoke_expired() == 1
    assert [item.session_id for item in broker.revoked] == [grant.session_id]
    stored = repository.records[grant.session_id]
    assert stored.status is MqttSessionStatus.EXPIRED
    assert stored.revoked_at == clock[0]
    assert service.revoke_expired() == 0


def test_issue_accepts_a_concurrently_activated_identical_session() -> None:
    now = datetime(2026, 9, 1, 8, tzinfo=UTC)
    repository = ConcurrentActivationRepository()
    service = TerminalMqttSessionService(
        lambda: MemoryUnitOfWork(repository),
        FakeBroker(),
        MqttSessionPasswordSigner("test-mqtt-session-signing-key-at-least-32-bytes"),
        public_host="mqtt.example.test",
        public_port=8883,
        tls_enabled=True,
        now=lambda: now,
    )

    grant = service.issue(uuid4())

    assert repository.records[grant.session_id].status is MqttSessionStatus.ACTIVE
