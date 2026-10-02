"""Review deadlines are checked during actions, not only by a periodic cleaner."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class ReviewState(StrEnum):
    HOLD = "hold"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    COMPLETED = "completed"


FINAL = frozenset({ReviewState.REJECTED, ReviewState.EXPIRED, ReviewState.COMPLETED})


@dataclass(frozen=True)
class ReviewPolicy:
    state: ReviewState
    received_at: datetime
    pending_at: datetime | None = None
    approved_at: datetime | None = None
    finalized_at: datetime | None = None

    def deadline(self) -> datetime | None:
        starts = {
            ReviewState.HOLD: self.received_at,
            ReviewState.PENDING: self.pending_at,
            ReviewState.APPROVED: self.approved_at,
        }
        if self.state in FINAL:
            return None
        start = starts[self.state]
        if start is None:
            raise ValueError("review_state_missing_timestamp")
        return start + timedelta(days=7)

    def expired(self, now: datetime) -> bool:
        deadline = self.deadline()
        return deadline is not None and now >= deadline

    def can_approve(self, now: datetime) -> bool:
        return self.state == ReviewState.PENDING and not self.expired(now)

    def can_send(self, now: datetime) -> bool:
        return self.state == ReviewState.APPROVED and not self.expired(now)

    def can_delete(self) -> bool:
        return self.state in FINAL

    def summary_expired(self, now: datetime) -> bool:
        return (self.state in FINAL and self.finalized_at is not None
                and now >= self.finalized_at + timedelta(days=30))
