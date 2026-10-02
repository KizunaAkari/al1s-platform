from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from al1s.execution.ports import ExecutionUnitOfWork
from al1s.kernel.types import NewOutboxEvent


class TerminalHealthMonitor:
    def __init__(self, uow_factory: Callable[[], ExecutionUnitOfWork], *,
                 timeout_seconds: int = 90,
                 now: Callable[[], datetime] | None = None):
        if timeout_seconds < 30:
            raise ValueError("heartbeat timeout must be at least 30 seconds")
        self._uow_factory = uow_factory
        self._timeout = timedelta(seconds=timeout_seconds)
        self._now = now or (lambda: datetime.now(UTC))

    def check_once(self) -> int:
        now = self._now()
        with self._uow_factory() as uow:
            expired = uow.terminals.expire_heartbeats(now - self._timeout, limit=100)
            for terminal in expired:
                uow.outbox.add(NewOutboxEvent(
                    event_id=uuid4(), event_type="terminal.offline.v1", schema_version=1,
                    aggregate_type="terminal", aggregate_id=terminal.terminal_id,
                    correlation_id=uuid4(), occurred_at=now,
                    payload={"reason": "heartbeat_timeout", "row_version": terminal.row_version},
                ))
            uow.commit()
        return len(expired)
