from datetime import datetime
from typing import Protocol
from uuid import UUID


class ReviewRecord(Protocol):
    id: UUID
    service_id: UUID
    message_id: str
    source_digest: str
    state: str
    body_cipher: str | None
    key_id: str | None
    lexicon_commit: str | None
    normalization_version: str | None
    received_at: datetime
    checked_at: datetime
    pending_at: datetime | None
    approved_at: datetime | None
    finalized_at: datetime | None
    deleted_at: datetime | None
    row_version: int
    intent_id: UUID | None


class ReviewRepository(Protocol):
    def get(self, review_id: UUID) -> ReviewRecord | None: ...
    def by_source(self, service_id: UUID, message_id: str) -> ReviewRecord | None: ...
    def add(self, values: dict[str, object]) -> bool: ...
    def add_many(self, rows: list[dict[str, object]]) -> set[UUID]: ...
    def by_sources(self, service_id: UUID, message_ids: list[str]) -> list[ReviewRecord]: ...
    def page(
        self, state: str | None, before: datetime | None, before_id: UUID | None, limit: int
    ) -> list[ReviewRecord]: ...
    def batch(self, ids: list[UUID]) -> list[ReviewRecord]: ...
    def maintenance(self, limit: int = 50) -> list[ReviewRecord]: ...
    def unsettled(self, ids: list[UUID]) -> set[UUID]: ...
    def cancel_unsettled(self, ids: list[UUID], now: datetime) -> None: ...
