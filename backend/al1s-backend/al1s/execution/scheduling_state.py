from __future__ import annotations

from al1s.execution.errors import ConflictError
from al1s.execution.scheduling_types import (
    AttemptStatus,
    ExecutionStatus,
    ScheduleStatus,
)

_SCHEDULE_TRANSITIONS: dict[ScheduleStatus, frozenset[ScheduleStatus]] = {
    ScheduleStatus.ACTIVE: frozenset(
        {ScheduleStatus.PAUSED, ScheduleStatus.TERMINATING, ScheduleStatus.COMPLETED}
    ),
    ScheduleStatus.PAUSED: frozenset(
        {ScheduleStatus.ACTIVE, ScheduleStatus.TERMINATING, ScheduleStatus.COMPLETED}
    ),
    ScheduleStatus.TERMINATING: frozenset({ScheduleStatus.TERMINATED}),
    ScheduleStatus.TERMINATED: frozenset(),
    ScheduleStatus.COMPLETED: frozenset(),
}

_EXECUTION_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.WAITING: frozenset(
        {ExecutionStatus.QUEUED, ExecutionStatus.ENDED, ExecutionStatus.CANCELLED}
    ),
    ExecutionStatus.QUEUED: frozenset(
        {ExecutionStatus.RUNNING, ExecutionStatus.ENDED, ExecutionStatus.CANCELLED}
    ),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.WAITING,
            ExecutionStatus.QUEUED,
            ExecutionStatus.ENDED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMED_OUT,
        }
    ),
    ExecutionStatus.ENDED: frozenset(),
    ExecutionStatus.CANCELLED: frozenset(),
    ExecutionStatus.TIMED_OUT: frozenset(),
}

_ATTEMPT_TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.QUEUED: frozenset(
        {AttemptStatus.RUNNING, AttemptStatus.ENDED, AttemptStatus.CANCELLED}
    ),
    AttemptStatus.RUNNING: frozenset(
        {AttemptStatus.ENDED, AttemptStatus.CANCELLED, AttemptStatus.TIMED_OUT}
    ),
    AttemptStatus.ENDED: frozenset(),
    AttemptStatus.CANCELLED: frozenset(),
    AttemptStatus.TIMED_OUT: frozenset(),
}


def require_schedule_transition(current: ScheduleStatus, target: ScheduleStatus) -> None:
    if target not in _SCHEDULE_TRANSITIONS[current]:
        _invalid_transition("task_schedule", current.value, target.value)


def require_execution_transition(current: ExecutionStatus, target: ExecutionStatus) -> None:
    if target not in _EXECUTION_TRANSITIONS[current]:
        _invalid_transition("execution", current.value, target.value)


def require_attempt_transition(current: AttemptStatus, target: AttemptStatus) -> None:
    if target not in _ATTEMPT_TRANSITIONS[current]:
        _invalid_transition("execution_attempt", current.value, target.value)


def _invalid_transition(aggregate: str, current: str, target: str) -> None:
    raise ConflictError(
        f"invalid_{aggregate}_transition",
        f"Cannot transition {aggregate} from {current} to {target}",
    )


def is_execution_terminal(status: ExecutionStatus) -> bool:
    return status in {
        ExecutionStatus.ENDED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.TIMED_OUT,
    }
