from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from al1s.execution.errors import ConflictError, InvalidRequestError, NotFoundError
from al1s.execution.schedule_calculation import timed_instants
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_types import (
    OccurrenceStatus,
    PlanOccurrenceRecord,
    ScheduleRevisionRecord,
    ScheduleRevisionResult,
    ScheduleRevisionTimeRecord,
    ScheduleStatus,
    StateTransitionRecord,
    TaskScheduleRecord,
    TaskType,
    TimedScheduleSpec,
    UpdateTimedScheduleCommand,
)
from al1s.kernel.types import NewAuditEntry, NewOutboxEvent

MAX_OCCURRENCES = 10_000


class ScheduleRevisionService:
    """Owns immutable timed-definition revisions and future-occurrence reconciliation."""

    def __init__(
        self,
        uow_factory: Callable[[], SchedulingUnitOfWork],
        now: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now

    def revise(
        self,
        schedule_id: UUID,
        command: UpdateTimedScheduleCommand,
        *,
        correlation_id: UUID,
    ) -> ScheduleRevisionResult:
        now = self._now()
        with self._uow_factory() as uow:
            schedule = self._require_schedule(uow, schedule_id, command.expected_version)
            current = uow.schedule_revisions.get_current(schedule)
            if current is None:
                raise ConflictError(
                    "schedule_revision_missing", "Timed schedule current revision is missing"
                )
            current_times = uow.schedule_revisions.list_times(current.schedule_revision_id)
            daily_times = tuple(sorted(set(command.daily_times)))
            spec = TimedScheduleSpec(
                timezone=current.timezone,
                start_date=command.start_date,
                end_date=command.end_date,
                daily_times=daily_times,
            )
            desired = timed_instants(spec, maximum=MAX_OCCURRENCES)
            if not desired or len(desired) > MAX_OCCURRENCES:
                raise InvalidRequestError(
                    "invalid_timed_occurrence_count",
                    f"Timed task must contain between 1 and {MAX_OCCURRENCES} occurrences",
                )
            if (
                current.start_date == command.start_date
                and current.end_date == command.end_date
                and current_times == daily_times
            ):
                return ScheduleRevisionResult(schedule, current, 0, 0, (), False)

            occurrences = uow.occurrences.list_for_schedule_update(schedule_id)
            if (
                any(item.status is OccurrenceStatus.MATERIALIZED for item in occurrences)
                and command.start_date != current.start_date
            ):
                raise ConflictError(
                    "schedule_start_date_locked",
                    "Timed schedule start date cannot change after execution has started",
                )

            desired_future = {instant for instant in desired if instant > now}
            materialized_future = {
                item.scheduled_for
                for item in occurrences
                if item.status is OccurrenceStatus.MATERIALIZED
                and item.scheduled_for is not None
                and item.scheduled_for > now
            }
            if not materialized_future.issubset(desired_future):
                raise ConflictError(
                    "materialized_occurrence_immutable",
                    "A materialized future occurrence cannot be removed from the schedule",
                )

            planned_future = {
                item.scheduled_for: item
                for item in occurrences
                if item.status is OccurrenceStatus.PLANNED
                and item.scheduled_for is not None
                and item.scheduled_for > now
            }
            removed = [
                item for instant, item in planned_future.items() if instant not in desired_future
            ]
            retained_times = (set(planned_future) | materialized_future) & desired_future
            added_times = sorted(desired_future - set(planned_future) - materialized_future)
            next_revision = (schedule.current_revision or 0) + 1
            revision = ScheduleRevisionRecord(
                schedule_revision_id=uuid4(),
                schedule_id=schedule.schedule_id,
                revision=next_revision,
                timezone=current.timezone,
                start_date=command.start_date,
                end_date=command.end_date,
                created_at=now,
            )
            revision_times = [
                ScheduleRevisionTimeRecord(uuid4(), revision.schedule_revision_id, ordinal, value)
                for ordinal, value in enumerate(daily_times, start=1)
            ]
            uow.schedule_revisions.add(revision, revision_times)
            cancelled = uow.occurrences.cancel_by_ids([item.occurrence_id for item in removed], now)
            if len(cancelled) != len(removed):
                raise ConflictError(
                    "schedule_occurrence_conflict", "A future occurrence changed concurrently"
                )
            next_ordinal = max((item.ordinal for item in occurrences), default=0) + 1
            added = tuple(
                _new_occurrence(
                    schedule,
                    revision.schedule_revision_id,
                    next_ordinal + offset,
                    instant,
                    now,
                )
                for offset, instant in enumerate(added_times)
            )
            uow.occurrences.add_many(added)
            total_occurrences = (
                sum(item.status is not OccurrenceStatus.CANCELLED for item in occurrences)
                - len(cancelled)
                + len(added)
            )
            updated = uow.schedules.set_timed_definition(
                schedule.schedule_id,
                schedule.row_version,
                total_occurrences=total_occurrences,
                start_date=command.start_date,
                end_date=command.end_date,
                current_revision=next_revision,
                now=now,
            )
            if updated is None:
                raise ConflictError("stale_schedule_version", "Task schedule changed concurrently")
            transitions = [
                _transition(
                    item.occurrence_id,
                    OccurrenceStatus.PLANNED.value,
                    OccurrenceStatus.CANCELLED.value,
                    correlation_id,
                    now,
                )
                for item in cancelled
            ]
            transitions.extend(
                _transition(
                    item.occurrence_id,
                    None,
                    OccurrenceStatus.PLANNED.value,
                    correlation_id,
                    now,
                )
                for item in added
            )
            transitions.append(
                StateTransitionRecord(
                    transition_id=uuid4(),
                    aggregate_type="task_schedule",
                    aggregate_id=schedule.schedule_id,
                    from_status=schedule.status.value,
                    to_status=updated.status.value,
                    actor_type="operator",
                    actor_id=None,
                    reason_code="schedule_definition_revised",
                    correlation_id=correlation_id,
                    occurred_at=now,
                )
            )
            uow.transitions.add_many(transitions)
            _record_change(
                uow,
                updated,
                revision,
                retained=len(retained_times),
                cancelled=len(cancelled),
                added=len(added),
                correlation_id=correlation_id,
                now=now,
            )
            uow.commit()
        return ScheduleRevisionResult(
            updated,
            revision,
            len(retained_times),
            len(cancelled),
            added,
            True,
        )

    @staticmethod
    def _require_schedule(
        uow: SchedulingUnitOfWork, schedule_id: UUID, expected_version: int
    ) -> TaskScheduleRecord:
        schedule = uow.schedules.get_for_update(schedule_id)
        if schedule is None:
            raise NotFoundError("task_schedule")
        if schedule.row_version != expected_version:
            raise ConflictError("stale_schedule_version", "Task schedule changed concurrently")
        if schedule.schedule_type is not TaskType.TIMED:
            raise ConflictError(
                "schedule_revision_requires_timed", "Only timed schedules can be revised"
            )
        if schedule.status not in {ScheduleStatus.ACTIVE, ScheduleStatus.PAUSED}:
            raise ConflictError(
                "schedule_not_revisable", "Only active or paused schedules can be revised"
            )
        return schedule


def _new_occurrence(
    schedule: TaskScheduleRecord,
    revision_id: UUID,
    ordinal: int,
    scheduled_for: datetime,
    now: datetime,
) -> PlanOccurrenceRecord:
    return PlanOccurrenceRecord(
        occurrence_id=uuid4(),
        task_request_id=schedule.task_request_id,
        schedule_id=schedule.schedule_id,
        schedule_revision_id=revision_id,
        ordinal=ordinal,
        scheduled_for=scheduled_for,
        status=OccurrenceStatus.PLANNED,
        materialization_owner=None,
        materialization_expires_at=None,
        created_at=now,
        settled_at=None,
        row_version=1,
    )


def _transition(
    occurrence_id: UUID,
    from_status: str | None,
    to_status: str,
    correlation_id: UUID,
    now: datetime,
) -> StateTransitionRecord:
    return StateTransitionRecord(
        transition_id=uuid4(),
        aggregate_type="plan_occurrence",
        aggregate_id=occurrence_id,
        from_status=from_status,
        to_status=to_status,
        actor_type="operator",
        actor_id=None,
        reason_code="schedule_definition_revised",
        correlation_id=correlation_id,
        occurred_at=now,
    )


def _record_change(
    uow: SchedulingUnitOfWork,
    schedule: TaskScheduleRecord,
    revision: ScheduleRevisionRecord,
    *,
    retained: int,
    cancelled: int,
    added: int,
    correlation_id: UUID,
    now: datetime,
) -> None:
    payload: dict[str, object] = {
        "schedule_id": str(schedule.schedule_id),
        "task_id": str(schedule.task_request_id),
        "revision": revision.revision,
        "retained_occurrences": retained,
        "cancelled_occurrences": cancelled,
        "added_occurrences": added,
    }
    uow.outbox.add(
        NewOutboxEvent(
            event_id=uuid4(),
            event_type="execution.schedule_revised.v1",
            schema_version=1,
            aggregate_type="task_schedule",
            aggregate_id=schedule.schedule_id,
            correlation_id=correlation_id,
            occurred_at=now,
            payload=payload,
        )
    )
    uow.audit.add(
        NewAuditEntry(
            audit_id=uuid4(),
            actor_type="operator",
            actor_id=None,
            action="execution.schedule.revise",
            target_type="task_schedule",
            target_id=schedule.schedule_id,
            correlation_id=correlation_id,
            details={"reason_code": "schedule_definition_revised", **payload},
            summary="schedule_definition_revised",
        )
    )
