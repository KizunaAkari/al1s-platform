from collections.abc import Callable
from threading import BoundedSemaphore, Lock
from typing import TypeVar

T = TypeVar("T")


class RenderPool:
    def __init__(self, limit: int = 2) -> None:
        self._slots = BoundedSemaphore(limit)
        self._lock = Lock()
        self._active = 0

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def run(self, operation: Callable[[], T]) -> T:
        if not self._slots.acquire(blocking=False):
            raise ValueError("render_busy")
        with self._lock:
            self._active += 1
        try:
            return operation()
        finally:
            with self._lock:
                self._active -= 1
            self._slots.release()
