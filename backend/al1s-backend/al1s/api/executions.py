from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from al1s.app.service_state import service_state
from al1s.execution.delivery_service import TerminalDeliveryService
from al1s.execution.delivery_types import CommandStatus, PackageStatus
from al1s.execution.scheduling_service import ExecutionSchedulingService
from al1s.execution.scheduling_types import (
    AttemptStatus,
    ExecutionDetails,
    ExecutionResult,
    ExecutionStatus,
    RetryOriginalSnapshotCommand,
)

router = APIRouter()


class ExecutionAttemptResponse(BaseModel):
    attempt_id: UUID
    attempt_no: int
    status: AttemptStatus
    result: ExecutionResult | None
    available_at: datetime
    enqueued_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    error_code: str | None
    retryable: bool | None
    failure_phase: str | None


class ExecutionSnapshotSummaryResponse(BaseModel):
    snapshot_id: UUID
    source_module: str
    logical_content_id: str
    revision_id: str
    schema_version: int
    manifest_hash: str
    terminal_id: UUID
    target_device_id: UUID | None
    timeout_seconds: int
    max_retries: int
    record_video: bool
    created_at: datetime


class StateTransitionResponse(BaseModel):
    transition_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    from_status: str | None
    to_status: str
    actor_type: str
    reason_code: str
    correlation_id: UUID
    occurred_at: datetime


class ExecutionDetailsResponse(BaseModel):
    execution_id: UUID
    task_request_id: UUID
    occurrence_id: UUID
    terminal_id: UUID
    target_device_id: UUID | None
    status: ExecutionStatus
    result: ExecutionResult | None
    timeout_at: datetime | None
    cancel_requested_at: datetime | None
    cancel_reason: str | None
    cancel_deadline_at: datetime | None
    cancel_delivery_delayed: bool
    cancel_delivery_note: str | None
    created_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    row_version: int
    attempts: list[ExecutionAttemptResponse]
    snapshot: ExecutionSnapshotSummaryResponse
    transitions: list[StateTransitionResponse]


class OriginalSnapshotRetryRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=160)


class OriginalSnapshotRetryResponse(BaseModel):
    task_id: UUID
    occurrence_id: UUID
    execution_id: UUID
    attempt_id: UUID
    snapshot_id: UUID
    source_execution_id: UUID
    source_snapshot_id: UUID


class BlockedRedeliveryRequest(BaseModel):
    expected_version: int = Field(ge=1)


class BlockedRedeliveryResponse(BaseModel):
    execution_id: UUID
    execution_row_version: int
    package_id: UUID
    package_hash: str
    package_status: PackageStatus
    command_id: UUID
    command_status: CommandStatus
    delivery_no: int


def _service(request: Request) -> ExecutionSchedulingService:
    return service_state(request).execution_scheduling


def _delivery(request: Request) -> TerminalDeliveryService:
    return service_state(request).terminal_delivery


def _response(details: ExecutionDetails) -> ExecutionDetailsResponse:
    execution = details.execution
    snapshot = details.snapshot
    return ExecutionDetailsResponse(
        execution_id=execution.execution_id,
        task_request_id=execution.task_request_id,
        occurrence_id=execution.occurrence_id,
        terminal_id=execution.terminal_id,
        target_device_id=execution.target_device_id,
        status=execution.status,
        result=execution.result,
        timeout_at=execution.timeout_at,
        cancel_requested_at=execution.cancel_requested_at,
        cancel_reason=execution.cancel_reason,
        cancel_deadline_at=execution.cancel_deadline_at,
        cancel_delivery_delayed=execution.cancel_delivery_delayed,
        cancel_delivery_note=execution.cancel_delivery_note,
        created_at=execution.created_at,
        queued_at=execution.queued_at,
        started_at=execution.started_at,
        ended_at=execution.ended_at,
        row_version=execution.row_version,
        attempts=[
            ExecutionAttemptResponse(
                attempt_id=item.attempt_id,
                attempt_no=item.attempt_no,
                status=item.status,
                result=item.result,
                available_at=item.available_at,
                enqueued_at=item.enqueued_at,
                started_at=item.started_at,
                ended_at=item.ended_at,
                error_code=item.error_code,
                retryable=item.retryable,
                failure_phase=item.failure_phase.value if item.failure_phase else None,
            )
            for item in details.attempts
        ],
        snapshot=ExecutionSnapshotSummaryResponse(
            snapshot_id=snapshot.snapshot_id,
            source_module=snapshot.source_module,
            logical_content_id=snapshot.logical_content_id,
            revision_id=snapshot.revision_id,
            schema_version=snapshot.schema_version,
            manifest_hash=snapshot.manifest_hash,
            terminal_id=snapshot.terminal_id,
            target_device_id=snapshot.target_device_id,
            timeout_seconds=snapshot.timeout_seconds,
            max_retries=snapshot.max_retries,
            record_video=snapshot.record_video,
            created_at=snapshot.created_at,
        ),
        transitions=[
            StateTransitionResponse(
                transition_id=item.transition_id,
                aggregate_type=item.aggregate_type,
                aggregate_id=item.aggregate_id,
                from_status=item.from_status,
                to_status=item.to_status,
                actor_type=item.actor_type,
                reason_code=item.reason_code,
                correlation_id=item.correlation_id,
                occurred_at=item.occurred_at,
            )
            for item in details.transitions
        ],
    )


@router.get("/{execution_id}", response_model=ExecutionDetailsResponse)
def get_execution(execution_id: UUID, request: Request) -> ExecutionDetailsResponse:
    return _response(_service(request).get_execution_details(execution_id))


@router.post(
    "/{execution_id}/redeliver-blocked-package",
    response_model=BlockedRedeliveryResponse,
)
def redeliver_blocked_package(
    execution_id: UUID,
    body: BlockedRedeliveryRequest,
    request: Request,
) -> BlockedRedeliveryResponse:
    result = _delivery(request).redeliver_blocked_execution(
        execution_id,
        expected_version=body.expected_version,
        correlation_id=_correlation_id(request),
    )
    return BlockedRedeliveryResponse(
        execution_id=result.execution.execution_id,
        execution_row_version=result.execution.row_version,
        package_id=result.package.package_id,
        package_hash=result.package.package_hash,
        package_status=result.package.status,
        command_id=result.command.command_id,
        command_status=result.command.status,
        delivery_no=result.command.delivery_no,
    )


@router.post(
    "/{execution_id}/retry-original-snapshot",
    response_model=OriginalSnapshotRetryResponse,
)
def retry_original_snapshot(
    execution_id: UUID,
    body: OriginalSnapshotRetryRequest,
    request: Request,
) -> OriginalSnapshotRetryResponse:
    result = _service(request).retry_original_snapshot(
        execution_id,
        RetryOriginalSnapshotCommand(
            idempotency_key=body.idempotency_key,
            name=body.name,
        ),
        correlation_id=_correlation_id(request),
    )
    return OriginalSnapshotRetryResponse(
        task_id=result.task.task_id,
        occurrence_id=result.occurrence.occurrence_id,
        execution_id=result.execution.execution_id,
        attempt_id=result.attempt.attempt_id,
        snapshot_id=result.snapshot.snapshot_id,
        source_execution_id=result.origin.source_execution_id,
        source_snapshot_id=result.origin.source_snapshot_id,
    )


def _correlation_id(request: Request) -> UUID:
    try:
        return UUID(request.state.request_id)
    except ValueError:
        return UUID(int=0)
