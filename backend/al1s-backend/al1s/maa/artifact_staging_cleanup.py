"""Bounded cleanup of staging copies after an artifact is settled."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class StagingCandidate:
    artifact_id: UUID
    terminal_id: UUID
    object_key: str
    expires_at: datetime
    completed_at: datetime
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class StagingSettlement:
    candidate: StagingCandidate
    next_at: datetime | None


class StagingRepository(Protocol):
    def claim(
        self, now: datetime, *, limit: int, lease: timedelta
    ) -> Sequence[StagingCandidate]: ...

    def settle_many(self, outcomes: Sequence[StagingSettlement]) -> set[UUID]: ...


class StagingObjectStore(Protocol):
    def delete(self, object_key: str) -> None: ...


class ArtifactStagingCleanup:
    def __init__(
        self,
        repository: StagingRepository,
        objects: StagingObjectStore,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.objects = objects
        self.now = now or (lambda: datetime.now(UTC))

    def run_once(self) -> tuple[int, int, int]:
        claimed = self.repository.claim(self.now(), limit=10, lease=timedelta(minutes=15))
        failed = 0
        outcomes: list[StagingSettlement] = []
        successful: set[UUID] = set()
        for candidate in claimed:
            outcome_at = self.now()
            expected_key = f"artifacts/{candidate.terminal_id}/{candidate.artifact_id}"
            if candidate.object_key != expected_key:
                failed += 1
                outcomes.append(
                    StagingSettlement(candidate, outcome_at + timedelta(minutes=15))
                )
                continue
            try:
                self.objects.delete(candidate.object_key)
            except Exception:
                failed += 1
                outcomes.append(
                    StagingSettlement(candidate, self.now() + timedelta(minutes=15))
                )
                continue
            cutoff = max(candidate.expires_at, candidate.completed_at) + timedelta(days=7)
            next_at = min(outcome_at + timedelta(days=1), cutoff) if outcome_at < cutoff else None
            successful.add(candidate.artifact_id)
            outcomes.append(StagingSettlement(candidate, next_at))
        settled = self.repository.settle_many(outcomes) if outcomes else set()
        return len(claimed), len(successful & settled), failed
