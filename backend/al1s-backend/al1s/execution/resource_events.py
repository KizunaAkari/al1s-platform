"""Record resource changes atomically in the caller's unit of work."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from al1s.execution.ports import ExecutionUnitOfWork
from al1s.kernel.types import NewAuditEntry, NewOutboxEvent


def record_resource_event(
    uow: ExecutionUnitOfWork,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    correlation_id: UUID,
    occurred_at: datetime,
    payload: dict[str, Any],
    action: str,
    actor_type: str,
    actor_id: UUID | None,
) -> None:
    uow.outbox.add(
        NewOutboxEvent(
            event_id=uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
            payload=payload,
        )
    )
    uow.audit.add(
        NewAuditEntry(
            audit_id=uuid4(),
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            target_type=aggregate_type,
            target_id=aggregate_id,
            correlation_id=correlation_id,
            details=payload,
        )
    )
