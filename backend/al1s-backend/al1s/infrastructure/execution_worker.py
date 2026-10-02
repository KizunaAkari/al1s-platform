from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4


class SchedulingRuntime(Protocol):
    def materialize_due(
        self,
        *,
        worker_id: UUID,
        correlation_id: UUID,
        limit: int = 50,
    ) -> Sequence[object]: ...


class TimeoutRuntime(Protocol):
    def request_timed_out_cancellation(self, *, limit: int = 50) -> int: ...

    def force_expired_cleanup(self, *, limit: int = 50) -> int: ...


@dataclass(frozen=True, slots=True)
class ExecutionWorkerCycleResult:
    materialized: int
    timed_out_cancellations: int
    forced_cleanups: int

    @property
    def changed(self) -> bool:
        return bool(self.materialized or self.timed_out_cancellations or self.forced_cleanups)


class ExecutionRuntimeWorker:
    """Runs the bounded scheduling and timeout maintenance for one platform process."""

    def __init__(
        self,
        scheduling: SchedulingRuntime,
        timeouts: TimeoutRuntime,
        *,
        worker_id: UUID | None = None,
        batch_size: int = 50,
    ) -> None:
        if not 1 <= batch_size <= 50:
            raise ValueError("batch_size must be between 1 and 50")
        self._scheduling = scheduling
        self._timeouts = timeouts
        self._worker_id = worker_id or uuid4()
        self._batch_size = batch_size

    def run_once(self) -> ExecutionWorkerCycleResult:
        try:
            materialized = self._scheduling.materialize_due(
                worker_id=self._worker_id,
                correlation_id=uuid4(),
                limit=self._batch_size,
            )
        finally:
            timed_out = self._timeouts.request_timed_out_cancellation(limit=self._batch_size)
            forced = self._timeouts.force_expired_cleanup(limit=self._batch_size)
        return ExecutionWorkerCycleResult(
            materialized=len(materialized),
            timed_out_cancellations=timed_out,
            forced_cleanups=forced,
        )
