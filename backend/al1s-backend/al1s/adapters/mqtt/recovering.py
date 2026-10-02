"""Lazy MQTT connection with bounded retry; construction never contacts the broker."""

import time
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol

from al1s.kernel.types import ClaimedOutboxEvent


class Publisher(Protocol):
    def publish(self, event: ClaimedOutboxEvent) -> None: ...
    def close(self) -> None: ...


class RecoveringPublisher:
    def __init__(
        self,
        factory: Callable[[], Publisher],
        *,
        clock: Callable[[], float] = time.monotonic,
        retry_seconds: float = 5,
    ) -> None:
        self._factory, self._clock, self._retry = factory, clock, retry_seconds
        self._publisher: Publisher | None = None
        self._retry_at = 0.0

    def publish(self, event: ClaimedOutboxEvent) -> None:
        if self._clock() < self._retry_at:
            raise ConnectionError("MQTT reconnect pending")
        try:
            if self._publisher is None:
                self._publisher = self._factory()
            self._publisher.publish(event)
        except Exception:
            self._retry_at = self._clock() + self._retry
            self.close()
            raise

    def close(self) -> None:
        publisher, self._publisher = self._publisher, None
        if publisher is not None:
            with suppress(Exception):
                publisher.close()
