from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class SetupFacts:
    secure_lock: bool
    provider_ready: bool
    battery_exempt: bool
    notifications_allowed: bool
    wireless_recovery_enabled: bool
    manufacturer_policy: str = "unverifiable"


@dataclass(frozen=True)
class SetupCheckRecord:
    request_id: UUID
    terminal_id: UUID
    status: str
    source: str
    requested_at: datetime
    claimed_at: datetime | None
    instance_id: UUID | None
    completed_at: datetime | None
    facts: SetupFacts | None


class SetupRepository(Protocol):
    def request(self, terminal: UUID, manual: bool, now: datetime) -> SetupCheckRecord | None: ...
    def claim(
        self, terminal: UUID, request: UUID, instance: UUID, now: datetime
    ) -> SetupCheckRecord: ...
    def complete(
        self, terminal: UUID, request: UUID, instance: UUID, facts: SetupFacts, now: datetime
    ) -> SetupCheckRecord: ...
    def latest(self, terminal: UUID) -> SetupCheckRecord | None: ...


class TerminalSetupCheckService:
    def __init__(self, repository: SetupRepository) -> None:
        self._repository = repository

    def request(self, terminal: UUID, *, manual: bool = False) -> SetupCheckRecord | None:
        return self._repository.request(terminal, manual, datetime.now(UTC))

    def claim(self, terminal: UUID, request: UUID, instance: UUID) -> SetupCheckRecord:
        return self._repository.claim(terminal, request, instance, datetime.now(UTC))

    def complete(
        self, terminal: UUID, request: UUID, instance: UUID, facts: SetupFacts
    ) -> SetupCheckRecord:
        return self._repository.complete(terminal, request, instance, facts, datetime.now(UTC))

    def latest(self, terminal: UUID) -> SetupCheckRecord | None:
        return self._repository.latest(terminal)
