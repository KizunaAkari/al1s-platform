from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from al1s.kernel.ports import EventPublisher, UnitOfWork
from al1s.kernel.types import DispatchBatchResult, FailedDelivery


class OutboxDispatcher:
    """Claims in one transaction, publishes outside it, then batch-updates outcomes."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        publisher: EventPublisher,
        worker_id: str,
        lease_duration: timedelta = timedelta(seconds=30),
        batch_size: int = 50,
        max_attempts: int = 8,
        base_retry_delay: timedelta = timedelta(seconds=5),
        max_retry_delay: timedelta = timedelta(minutes=15),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id is required")
        if not 1 <= batch_size <= 50:
            raise ValueError("batch_size must be between 1 and 50")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._uow_factory = uow_factory
        self._publisher = publisher
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._base_retry_delay = base_retry_delay
        self._max_retry_delay = max_retry_delay
        self._now = now or (lambda: datetime.now(UTC))

    def dispatch_once(self) -> DispatchBatchResult:
        claim_time = self._now()
        with self._uow_factory() as uow:
            claimed = uow.outbox.claim_batch(
                worker_id=self._worker_id,
                now=claim_time,
                lease_duration=self._lease_duration,
                limit=self._batch_size,
            )
            uow.commit()

        if not claimed:
            return DispatchBatchResult(claimed=0, published=0, failed=0, stale=0)

        published = []
        failed = []
        for event in claimed:
            # The whole batch has the same deadline. Never start another send
            # after it: a replacement worker may already own the remaining rows.
            if self._now() >= claim_time + self._lease_duration:
                break
            try:
                self._publisher.publish(event)
                published.append(event)
            except Exception as exc:  # adapter failures are classified without storing messages
                failed.append(
                    FailedDelivery(
                        event_id=event.event_id,
                        row_version=event.row_version,
                        attempt_count=event.attempt_count,
                        error_type=type(exc).__name__[:255],
                    )
                )

        outcome_time = self._now()
        with self._uow_factory() as uow:
            published_count = uow.outbox.mark_published(
                published,
                worker_id=self._worker_id,
                published_at=outcome_time,
            )
            failed_count = uow.outbox.mark_failed(
                failed,
                worker_id=self._worker_id,
                failed_at=outcome_time,
                max_attempts=self._max_attempts,
                base_retry_delay=self._base_retry_delay,
                max_retry_delay=self._max_retry_delay,
            )
            uow.commit()

        updated = published_count + failed_count
        return DispatchBatchResult(
            claimed=len(claimed),
            published=published_count,
            failed=failed_count,
            stale=len(claimed) - updated,
        )
