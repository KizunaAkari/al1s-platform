"""Settle delivery failures without consuming a script retry."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from al1s.execution.errors import ConflictError
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_state import require_attempt_transition, require_execution_transition
from al1s.execution.scheduling_types import (
    AttemptStatus,
    ExecutionAttemptRecord,
    ExecutionRecord,
    ExecutionResult,
    ExecutionStatus,
    FailurePhase,
)
from al1s.execution.task_settlement import settle_task_after_execution


def settle_delivery_failure(
    uow: SchedulingUnitOfWork,
    *,
    execution: ExecutionRecord,
    attempt: ExecutionAttemptRecord,
    error_code: str,
    correlation_id: UUID,
    now: datetime,
) -> tuple[ExecutionAttemptRecord, ExecutionRecord]:
    require_attempt_transition(attempt.status, AttemptStatus.ENDED)
    require_execution_transition(execution.status, ExecutionStatus.ENDED)
    updated_attempt = uow.attempts.set_status(
        attempt.attempt_id,
        attempt.row_version,
        AttemptStatus.ENDED.value,
        result=ExecutionResult.FAILURE.value,
        now=now,
        error_code=error_code,
        retryable=False,
        failure_phase=FailurePhase.DELIVERY.value,
    )
    updated_execution = uow.executions.set_status(
        execution.execution_id,
        execution.row_version,
        ExecutionStatus.ENDED.value,
        result=ExecutionResult.FAILURE.value,
        now=now,
        timeout_at=None,
    )
    if updated_attempt is None or updated_execution is None:
        raise ConflictError("delivery_failure_conflict", "Delivery failure changed concurrently")
    schedule = uow.schedules.get_by_task_for_update(execution.task_request_id)
    settle_task_after_execution(
        uow,
        task_id=execution.task_request_id,
        schedule=schedule,
        correlation_id=correlation_id,
        now=now,
    )
    return updated_attempt, updated_execution
