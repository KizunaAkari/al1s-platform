"""One bounded manual cleanup request, executed by the existing media worker."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class StorageGcRequest:
    id: UUID
    status: str
    requested_at: datetime
    completed_at: datetime | None
    attempt_count: int
    claimed: int
    deleted: int
    failed: int
    stale: int
    error_type: str | None


@dataclass(frozen=True, slots=True)
class ClaimedStorageGcRequest:
    id: UUID
    attempt_count: int


class StorageGcRepository(Protocol):
    def latest(self) -> StorageGcRequest | None: ...
    def submit(self) -> StorageGcRequest: ...


class StorageGcService:
    def __init__(self, repository: StorageGcRepository) -> None:
        self._repository = repository

    def latest(self) -> StorageGcRequest | None:
        return self._repository.latest()

    def submit(self) -> StorageGcRequest:
        return self._repository.submit()
