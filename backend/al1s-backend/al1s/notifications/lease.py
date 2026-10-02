"""Keep bounded notification batches leased without holding a transaction during I/O."""

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from threading import Event, Thread
from types import TracebackType

from al1s.notifications.ports import NotificationUnitOfWork
from al1s.notifications.types import ClaimedNotificationDelivery


class DeliveryLease:
    def __init__(
        self,
        factory: Callable[[], NotificationUnitOfWork],
        deliveries: Sequence[ClaimedNotificationDelivery],
        *,
        worker_id: str,
        duration: timedelta,
        now: Callable[[], datetime],
    ) -> None:
        self._factory = factory
        self._deliveries = tuple(deliveries)
        self._worker_id = worker_id
        self._duration = duration
        self._now = now
        self._valid_until = min(item.started_at or now() for item in deliveries) + duration
        self._stop = Event()
        self._lost = Event()
        self._thread = Thread(target=self._run, name="notification-lease", daemon=True)

    @property
    def valid(self) -> bool:
        return not self._lost.is_set() and self._now() < self._valid_until

    def renew(self) -> bool:
        if not self.valid:
            return False
        try:
            renewed_at = self._now()
            with self._factory() as uow:
                renewed = uow.deliveries.renew_batch(
                    self._deliveries,
                    worker_id=self._worker_id,
                    now=renewed_at,
                    lease_duration=self._duration,
                )
                if renewed != len(self._deliveries):
                    self._lost.set()
                    return False
                uow.commit()
            self._valid_until = renewed_at + self._duration
        except Exception:
            # Never log database parameters or continue starting sends after lease failure.
            self._lost.set()
        return self.valid

    def _run(self) -> None:
        while not self._stop.wait(self._duration.total_seconds() / 3):
            if not self.renew():
                return

    def __enter__(self) -> "DeliveryLease":
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()
