from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, Field, model_validator

from al1s.api.cursors import decode_timestamp_cursor, encode_timestamp_cursor
from al1s.app.service_state import service_state
from al1s.execution.scheduling_service import ExecutionSchedulingService
from al1s.execution.scheduling_types import (
    CreatedTask,
    CreateTaskCommand,
    ExecutionResult,
    ExecutionStatus,
    LoopScheduleSpec,
    ScheduleStatus,
    TaskCancellation,
    TaskHistoryManagementSummary,
    TaskLifecycleStatus,
    TaskType,
    TimedScheduleSpec,
)
from al1s.execution.types import ActionAvailability

router = APIRouter()


class LoopScheduleRequest(BaseModel):
    repeat_count: int = Field(ge=1, le=10_000)


class TimedScheduleRequest(BaseModel):
    timezone: str = Field(min_length=1, max_length=100)
    start_date: date
    end_date: date
    daily_times: list[time] = Field(min_length=1, max_length=48)


class CreateTaskRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=160)
    task_type: TaskType
    source_module: str = Field(min_length=1, max_length=80)
    logical_content_id: str = Field(min_length=1, max_length=255)
    parameters: dict[str, Any] = Field(default_factory=dict)
    requested_terminal_id: UUID | None = None
    requested_target_device_id: UUID | None = None
    timeout_seconds: int = Field(default=1800, ge=1, le=14_400)
    max_retries: int = Field(default=0, ge=0, le=100)
    record_video: bool = False
    loop: LoopScheduleRequest | None = None
    timed: TimedScheduleRequest | None = None

    @model_validator(mode="after")
    def validate_schedule_shape(self) -> CreateTaskRequest:
        valid = (
            (self.task_type is TaskType.SINGLE and self.loop is None and self.timed is None)
            or (self.task_type is TaskType.LOOP and self.loop is not None and self.timed is None)
            or (self.task_type is TaskType.TIMED and self.timed is not None and self.loop is None)
        )
        if not valid:
            raise ValueError("task type and schedule definition do not match")
        return self


class TaskResponse(BaseModel):
    task_id: UUID
    name: str
    task_type: TaskType
    lifecycle_status: TaskLifecycleStatus
    schedule_id: UUID | None
    schedule_status: ScheduleStatus | None
    occurrence_count: int
    created_at: datetime
    row_version: int


class ActionAvailabilityResponse(BaseModel):
    allowed: bool
    refusal_code: str | None
    refusal_message: str | None


class BatchHistoryResponse(BaseModel):
    image_count: int
    execution_status: ExecutionStatus
    result: ExecutionResult | None


class TaskHistoryItemResponse(BaseModel):
    task_id: UUID
    name: str
    source_module: str = ""
    logical_content_id: str = ""
    task_type: TaskType
    lifecycle_status: TaskLifecycleStatus
    schedule_status: str | None
    latest_execution_id: UUID | None
    latest_execution_status: ExecutionStatus | None
    latest_result: ExecutionResult | None
    batch_summary: BatchHistoryResponse | None = None
    record_video: bool
    created_at: datetime
    completed_at: datetime | None
    row_version: int
    cancel: ActionAvailabilityResponse
    delete: ActionAvailabilityResponse


class TaskHistoryPageResponse(BaseModel):
    items: list[TaskHistoryItemResponse]
    next_cursor: str | None


class TaskMutationRequest(BaseModel):
    expected_version: int = Field(ge=1)


class TaskCancellationResponse(BaseModel):
    task_id: UUID
    lifecycle_status: TaskLifecycleStatus
    execution_id: UUID | None
    execution_status: ExecutionStatus | None
    pending_terminal_ack: bool
    row_version: int


def _service(request: Request) -> ExecutionSchedulingService:
    return service_state(request).execution_scheduling


def _correlation_id(request: Request) -> UUID:
    try:
        return UUID(request.state.request_id)
    except ValueError:
        return UUID(int=0)


def _task_response(task: CreatedTask) -> TaskResponse:
    return TaskResponse(
        task_id=task.task.task_id,
        name=task.task.name,
        task_type=task.task.task_type,
        lifecycle_status=task.task.lifecycle_status,
        schedule_id=task.schedule.schedule_id if task.schedule else None,
        schedule_status=task.schedule.status if task.schedule else None,
        occurrence_count=len(task.occurrences),
        created_at=task.task.created_at,
        row_version=task.task.row_version,
    )


def _action_response(action: ActionAvailability) -> ActionAvailabilityResponse:
    return ActionAvailabilityResponse(
        allowed=action.allowed,
        refusal_code=action.refusal_code,
        refusal_message=action.refusal_message,
    )


def _history_response(item: TaskHistoryManagementSummary) -> TaskHistoryItemResponse:
    task = item.task
    return TaskHistoryItemResponse(
        source_module=task.source_module,
        logical_content_id=task.logical_content_id,
        task_id=task.task_id,
        name=task.name,
        task_type=task.task_type,
        lifecycle_status=task.lifecycle_status,
        schedule_status=task.schedule_status.value if task.schedule_status else None,
        latest_execution_id=task.latest_execution_id,
        latest_execution_status=task.latest_execution_status,
        latest_result=task.latest_result,
        batch_summary=(
            BatchHistoryResponse(
                image_count=task.batch_summary.image_count,
                execution_status=task.batch_summary.execution_status,
                result=task.batch_summary.result,
            )
            if task.batch_summary
            else None
        ),
        record_video=task.record_video,
        created_at=task.created_at,
        completed_at=task.completed_at,
        row_version=task.row_version,
        cancel=_action_response(item.cancel),
        delete=_action_response(item.delete),
    )


def _cancellation_response(item: TaskCancellation) -> TaskCancellationResponse:
    return TaskCancellationResponse(
        task_id=item.task.task_id,
        lifecycle_status=item.task.lifecycle_status,
        execution_id=item.execution.execution_id if item.execution else None,
        execution_status=item.execution.status if item.execution else None,
        pending_terminal_ack=item.pending_terminal_ack,
        row_version=item.task.row_version,
    )


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(body: CreateTaskRequest, request: Request) -> TaskResponse:
    result = _service(request).create_task(
        CreateTaskCommand(
            idempotency_key=body.idempotency_key,
            name=body.name,
            task_type=body.task_type,
            source_module=body.source_module,
            logical_content_id=body.logical_content_id,
            parameters=body.parameters,
            requested_terminal_id=body.requested_terminal_id,
            requested_target_device_id=body.requested_target_device_id,
            timeout_seconds=body.timeout_seconds,
            max_retries=body.max_retries,
            record_video=body.record_video,
            loop=(LoopScheduleSpec(body.loop.repeat_count) if body.loop is not None else None),
            timed=(
                TimedScheduleSpec(
                    timezone=body.timed.timezone,
                    start_date=body.timed.start_date,
                    end_date=body.timed.end_date,
                    daily_times=tuple(body.timed.daily_times),
                )
                if body.timed is not None
                else None
            ),
        ),
        correlation_id=_correlation_id(request),
    )
    return _task_response(result)


@router.get("", response_model=TaskHistoryPageResponse)
def list_task_history(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> TaskHistoryPageResponse:
    before_created_at, before_id = decode_timestamp_cursor(cursor)
    items = _service(request).list_task_history(
        before_created_at=before_created_at,
        before_id=before_id,
        limit=limit,
    )
    return TaskHistoryPageResponse(
        items=[_history_response(item) for item in items],
        next_cursor=(
            encode_timestamp_cursor(items[-1].task.created_at, items[-1].task.task_id)
            if len(items) == limit
            else None
        ),
    )


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: UUID, request: Request) -> TaskResponse:
    return _task_response(_service(request).get_task(task_id))


@router.post("/{task_id}/cancel", response_model=TaskCancellationResponse)
def cancel_single_task(
    task_id: UUID, body: TaskMutationRequest, request: Request
) -> TaskCancellationResponse:
    return _cancellation_response(
        _service(request).cancel_single_task(
            task_id,
            expected_version=body.expected_version,
            correlation_id=_correlation_id(request),
        )
    )


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task_history(task_id: UUID, body: TaskMutationRequest, request: Request) -> None:
    _service(request).delete_task_history(
        task_id,
        expected_version=body.expected_version,
        correlation_id=_correlation_id(request),
    )
