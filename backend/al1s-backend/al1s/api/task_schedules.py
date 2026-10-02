from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from al1s.api.cursors import (
    decode_ordinal_cursor,
    decode_timestamp_cursor,
    encode_ordinal_cursor,
    encode_timestamp_cursor,
)
from al1s.app.service_state import service_state
from al1s.execution.scheduling_service import ExecutionSchedulingService
from al1s.execution.scheduling_types import (
    ActiveScheduleManagementSummary,
    ExecutionResult,
    ExecutionStatus,
    OccurrenceStatus,
    OccurrenceSummary,
    ScheduleRevisionResult,
    ScheduleStatus,
    TaskScheduleRecord,
    TaskType,
    UpdateTimedScheduleCommand,
)
from al1s.execution.types import ActionAvailability


class ActionAvailabilityResponse(BaseModel):
    allowed: bool
    refusal_code: str | None
    refusal_message: str | None


router = APIRouter()


class ScheduleControlRequest(BaseModel):
    expected_version: int = Field(ge=1)


class ScheduleResponse(BaseModel):
    schedule_id: UUID
    task_request_id: UUID
    schedule_type: TaskType
    status: ScheduleStatus
    total_occurrences: int
    current_revision: int | None
    row_version: int
    paused_at: datetime | None
    completed_at: datetime | None


class ScheduleSort(StrEnum):
    ASC = "asc"
    DESC = "desc"


class OccurrenceScope(StrEnum):
    CURRENT = "current"
    HISTORY = "history"


class ActiveScheduleItemResponse(BaseModel):
    task_id: UUID
    schedule_id: UUID
    name: str
    schedule_type: TaskType
    status: ScheduleStatus
    timezone: str | None
    start_date: date | None
    end_date: date | None
    daily_times: list[time]
    current_revision: int | None
    total_occurrences: int
    settled_occurrences: int
    planned_occurrences: int
    materialized_occurrences: int
    skipped_occurrences: int
    cancelled_occurrences: int
    current_occurrence_ordinal: int | None
    next_occurrence_ordinal: int | None
    row_version: int
    updated_at: datetime
    pause: ActionAvailabilityResponse
    resume: ActionAvailabilityResponse
    terminate: ActionAvailabilityResponse
    revise: ActionAvailabilityResponse


class ActiveSchedulePageResponse(BaseModel):
    items: list[ActiveScheduleItemResponse]
    next_cursor: str | None


class OccurrenceItemResponse(BaseModel):
    occurrence_id: UUID
    schedule_revision_id: UUID | None
    schedule_revision: int | None
    display_ordinal: int | None
    history_ordinal: int
    scheduled_for: datetime | None
    status: OccurrenceStatus
    execution_id: UUID | None
    execution_status: ExecutionStatus | None
    execution_result: ExecutionResult | None


class OccurrencePageResponse(BaseModel):
    items: list[OccurrenceItemResponse]
    next_cursor: str | None


class TimedScheduleRevisionRequest(BaseModel):
    expected_version: int = Field(ge=1)
    start_date: date
    end_date: date
    daily_times: list[time] = Field(min_length=1, max_length=32)


class TimedScheduleRevisionResponse(BaseModel):
    schedule: ScheduleResponse
    revision: int
    changed: bool
    retained_occurrences: int
    cancelled_occurrences: int
    added_occurrences: int


def _service(request: Request) -> ExecutionSchedulingService:
    return service_state(request).execution_scheduling


def _correlation_id(request: Request) -> UUID:
    try:
        return UUID(request.state.request_id)
    except ValueError:
        return UUID(int=0)


def _response(schedule: TaskScheduleRecord) -> ScheduleResponse:
    return ScheduleResponse(
        schedule_id=schedule.schedule_id,
        task_request_id=schedule.task_request_id,
        schedule_type=schedule.schedule_type,
        status=schedule.status,
        total_occurrences=schedule.total_occurrences,
        current_revision=schedule.current_revision,
        row_version=schedule.row_version,
        paused_at=schedule.paused_at,
        completed_at=schedule.completed_at,
    )


def _action_response(action: ActionAvailability) -> ActionAvailabilityResponse:
    return ActionAvailabilityResponse(
        allowed=action.allowed,
        refusal_code=action.refusal_code,
        refusal_message=action.refusal_message,
    )


def _active_response(item: ActiveScheduleManagementSummary) -> ActiveScheduleItemResponse:
    schedule = item.schedule
    return ActiveScheduleItemResponse(
        task_id=schedule.task_id,
        schedule_id=schedule.schedule_id,
        name=schedule.name,
        schedule_type=schedule.schedule_type,
        status=schedule.status,
        timezone=schedule.timezone,
        start_date=schedule.start_date,
        end_date=schedule.end_date,
        daily_times=list(schedule.daily_times),
        current_revision=schedule.current_revision,
        total_occurrences=schedule.total_occurrences,
        settled_occurrences=schedule.settled_occurrences,
        planned_occurrences=schedule.planned_occurrences,
        materialized_occurrences=schedule.materialized_occurrences,
        skipped_occurrences=schedule.skipped_occurrences,
        cancelled_occurrences=schedule.cancelled_occurrences,
        current_occurrence_ordinal=schedule.current_occurrence_ordinal,
        next_occurrence_ordinal=schedule.next_occurrence_ordinal,
        row_version=schedule.row_version,
        updated_at=schedule.updated_at,
        pause=_action_response(item.pause),
        resume=_action_response(item.resume),
        terminate=_action_response(item.terminate),
        revise=_action_response(item.revise),
    )


def _occurrence_response(item: OccurrenceSummary) -> OccurrenceItemResponse:
    return OccurrenceItemResponse(
        occurrence_id=item.occurrence.occurrence_id,
        schedule_revision_id=item.occurrence.schedule_revision_id,
        schedule_revision=item.schedule_revision,
        display_ordinal=item.display_ordinal,
        history_ordinal=item.occurrence.ordinal,
        scheduled_for=item.occurrence.scheduled_for,
        status=item.occurrence.status,
        execution_id=item.execution_id,
        execution_status=item.execution_status,
        execution_result=item.execution_result,
    )


@router.get("", response_model=ActiveSchedulePageResponse)
def list_active_schedules(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    sort: ScheduleSort = ScheduleSort.ASC,
) -> ActiveSchedulePageResponse:
    cursor_updated_at, cursor_id = decode_timestamp_cursor(cursor)
    schedules = _service(request).list_active_schedules(
        cursor_updated_at=cursor_updated_at,
        cursor_id=cursor_id,
        limit=limit,
        descending=sort is ScheduleSort.DESC,
    )
    return ActiveSchedulePageResponse(
        items=[_active_response(item) for item in schedules],
        next_cursor=(
            encode_timestamp_cursor(
                schedules[-1].schedule.updated_at,
                schedules[-1].schedule.schedule_id,
            )
            if len(schedules) == limit
            else None
        ),
    )


@router.get("/{schedule_id}/occurrences", response_model=OccurrencePageResponse)
def list_schedule_occurrences(
    schedule_id: UUID,
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    scope: OccurrenceScope = OccurrenceScope.CURRENT,
) -> OccurrencePageResponse:
    after_ordinal, after_id = decode_ordinal_cursor(cursor)
    items = _service(request).list_schedule_occurrences(
        schedule_id,
        historical=scope is OccurrenceScope.HISTORY,
        after_ordinal=after_ordinal,
        after_id=after_id,
        limit=limit,
    )
    return OccurrencePageResponse(
        items=[_occurrence_response(item) for item in items],
        next_cursor=(
            encode_ordinal_cursor(
                (
                    items[-1].occurrence.ordinal
                    if scope is OccurrenceScope.HISTORY
                    else cast(int, items[-1].display_ordinal)
                ),
                items[-1].occurrence.occurrence_id,
            )
            if len(items) == limit
            else None
        ),
    )


@router.post("/{schedule_id}/pause", response_model=ScheduleResponse)
def pause_schedule(
    schedule_id: UUID, body: ScheduleControlRequest, request: Request
) -> ScheduleResponse:
    return _response(
        _service(request).pause_schedule(
            schedule_id,
            expected_version=body.expected_version,
            correlation_id=_correlation_id(request),
        )
    )


@router.post("/{schedule_id}/resume", response_model=ScheduleResponse)
def resume_schedule(
    schedule_id: UUID, body: ScheduleControlRequest, request: Request
) -> ScheduleResponse:
    return _response(
        _service(request).resume_schedule(
            schedule_id,
            expected_version=body.expected_version,
            correlation_id=_correlation_id(request),
        )
    )


@router.post("/{schedule_id}/terminate", response_model=ScheduleResponse)
def terminate_schedule(
    schedule_id: UUID, body: ScheduleControlRequest, request: Request
) -> ScheduleResponse:
    return _response(
        _service(request).terminate_schedule(
            schedule_id,
            expected_version=body.expected_version,
            correlation_id=_correlation_id(request),
        )
    )


def _revision_response(result: ScheduleRevisionResult) -> TimedScheduleRevisionResponse:
    return TimedScheduleRevisionResponse(
        schedule=_response(result.schedule),
        revision=result.revision.revision,
        changed=result.changed,
        retained_occurrences=result.retained_occurrences,
        cancelled_occurrences=result.cancelled_occurrences,
        added_occurrences=len(result.added_occurrences),
    )


@router.post(
    "/{schedule_id}/revision",
    response_model=TimedScheduleRevisionResponse,
)
def revise_timed_schedule(
    schedule_id: UUID,
    body: TimedScheduleRevisionRequest,
    request: Request,
) -> TimedScheduleRevisionResponse:
    result = _service(request).revise_timed_schedule(
        schedule_id,
        UpdateTimedScheduleCommand(
            expected_version=body.expected_version,
            start_date=body.start_date,
            end_date=body.end_date,
            daily_times=tuple(body.daily_times),
        ),
        correlation_id=_correlation_id(request),
    )
    return _revision_response(result)
