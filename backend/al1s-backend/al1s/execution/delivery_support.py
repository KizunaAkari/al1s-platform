from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID, uuid4

from al1s.execution.delivery_types import TerminalReportRecord
from al1s.execution.errors import ConflictError
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_types import StateTransitionRecord
from al1s.kernel.types import NewAuditEntry, NewOutboxEvent


def payload_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_same_report(report: TerminalReportRecord, expected_payload_hash: str) -> None:
    if report.payload_hash != expected_payload_hash:
        raise ConflictError("terminal_report_reused", "Report ID was reused with another payload")


def terminal_transition(
    aggregate_id: UUID,
    aggregate_type: str,
    from_status: str,
    to_status: str,
    correlation_id: UUID,
    now: datetime,
) -> StateTransitionRecord:
    return StateTransitionRecord(
        transition_id=uuid4(),
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        from_status=from_status,
        to_status=to_status,
        actor_type="terminal",
        actor_id=None,
        reason_code=f"{aggregate_type}_{to_status}",
        correlation_id=correlation_id,
        occurred_at=now,
    )


def record_delivery_event(
    uow: SchedulingUnitOfWork,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    correlation_id: UUID,
    occurred_at: datetime,
    reason_code: str,
    payload: dict[str, object],
    actor_type: str = "terminal",
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
            payload=dict(payload),
        )
    )
    uow.audit.add(
        NewAuditEntry(
            audit_id=uuid4(),
            actor_type=actor_type,
            actor_id=None,
            action=event_type.removesuffix(".v1"),
            target_type=aggregate_type,
            target_id=aggregate_id,
            correlation_id=correlation_id,
            details={"reason_code": reason_code, **payload},
            summary=reason_code,
        )
    )
