from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from al1s.infrastructure.execution_worker import ExecutionRuntimeWorker


class SchedulingSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID, int]] = []

    def materialize_due(
        self,
        *,
        worker_id: UUID,
        correlation_id: UUID,
        limit: int = 50,
    ) -> list[object]:
        self.calls.append((worker_id, correlation_id, limit))
        return [object(), object()]


class TimeoutSpy:
    def __init__(self) -> None:
        self.cancel_limits: list[int] = []
        self.cleanup_limits: list[int] = []

    def request_timed_out_cancellation(self, *, limit: int = 50) -> int:
        self.cancel_limits.append(limit)
        return 1

    def force_expired_cleanup(self, *, limit: int = 50) -> int:
        self.cleanup_limits.append(limit)
        return 3


def test_cycle_materializes_and_sweeps_timeouts_with_bounded_batches() -> None:
    scheduling = SchedulingSpy()
    timeouts = TimeoutSpy()
    worker_id = uuid4()
    worker = ExecutionRuntimeWorker(scheduling, timeouts, worker_id=worker_id, batch_size=20)

    first = worker.run_once()
    second = worker.run_once()

    assert first.materialized == 2
    assert first.timed_out_cancellations == 1
    assert first.forced_cleanups == 3
    assert first.changed is True
    assert [call[0] for call in scheduling.calls] == [worker_id, worker_id]
    assert scheduling.calls[0][1] != scheduling.calls[1][1]
    assert [call[2] for call in scheduling.calls] == [20, 20]
    assert timeouts.cancel_limits == [20, 20]
    assert timeouts.cleanup_limits == [20, 20]
    assert second.changed is True


def test_cycle_result_is_unchanged_when_no_work_exists() -> None:
    scheduling = SchedulingSpy()
    scheduling.materialize_due = lambda **_kwargs: []  # type: ignore[method-assign]
    timeouts = TimeoutSpy()
    timeouts.request_timed_out_cancellation = lambda **_kwargs: 0  # type: ignore[method-assign]
    timeouts.force_expired_cleanup = lambda **_kwargs: 0  # type: ignore[method-assign]

    result = ExecutionRuntimeWorker(scheduling, timeouts).run_once()

    assert result.changed is False


@pytest.mark.parametrize("batch_size", [0, 51])
def test_worker_rejects_unbounded_batch_size(batch_size: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 50"):
        ExecutionRuntimeWorker(SchedulingSpy(), TimeoutSpy(), batch_size=batch_size)
