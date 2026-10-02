from __future__ import annotations

import pytest

from al1s.execution.errors import ConflictError
from al1s.execution.scheduling_state import (
    is_execution_terminal,
    require_attempt_transition,
    require_execution_transition,
    require_schedule_transition,
)
from al1s.execution.scheduling_types import AttemptStatus, ExecutionStatus, ScheduleStatus

_SCHEDULE_ALLOWED = (
    (ScheduleStatus.ACTIVE, ScheduleStatus.PAUSED),
    (ScheduleStatus.ACTIVE, ScheduleStatus.TERMINATING),
    (ScheduleStatus.ACTIVE, ScheduleStatus.COMPLETED),
    (ScheduleStatus.PAUSED, ScheduleStatus.ACTIVE),
    (ScheduleStatus.PAUSED, ScheduleStatus.TERMINATING),
    (ScheduleStatus.PAUSED, ScheduleStatus.COMPLETED),
    (ScheduleStatus.TERMINATING, ScheduleStatus.TERMINATED),
)
_SCHEDULE_REJECTED = tuple(
    (current, target)
    for current in ScheduleStatus
    for target in ScheduleStatus
    if (current, target) not in _SCHEDULE_ALLOWED
)

_EXECUTION_ALLOWED = (
    (ExecutionStatus.WAITING, ExecutionStatus.QUEUED),
    (ExecutionStatus.WAITING, ExecutionStatus.ENDED),
    (ExecutionStatus.WAITING, ExecutionStatus.CANCELLED),
    (ExecutionStatus.QUEUED, ExecutionStatus.RUNNING),
    (ExecutionStatus.QUEUED, ExecutionStatus.ENDED),
    (ExecutionStatus.QUEUED, ExecutionStatus.CANCELLED),
    (ExecutionStatus.RUNNING, ExecutionStatus.QUEUED),
    (ExecutionStatus.RUNNING, ExecutionStatus.WAITING),
    (ExecutionStatus.RUNNING, ExecutionStatus.ENDED),
    (ExecutionStatus.RUNNING, ExecutionStatus.CANCELLED),
    (ExecutionStatus.RUNNING, ExecutionStatus.TIMED_OUT),
)
_EXECUTION_REJECTED = tuple(
    (current, target)
    for current in ExecutionStatus
    for target in ExecutionStatus
    if (current, target) not in _EXECUTION_ALLOWED
)

_ATTEMPT_ALLOWED = (
    (AttemptStatus.QUEUED, AttemptStatus.RUNNING),
    (AttemptStatus.QUEUED, AttemptStatus.ENDED),
    (AttemptStatus.QUEUED, AttemptStatus.CANCELLED),
    (AttemptStatus.RUNNING, AttemptStatus.ENDED),
    (AttemptStatus.RUNNING, AttemptStatus.CANCELLED),
    (AttemptStatus.RUNNING, AttemptStatus.TIMED_OUT),
)
_ATTEMPT_REJECTED = tuple(
    (current, target)
    for current in AttemptStatus
    for target in AttemptStatus
    if (current, target) not in _ATTEMPT_ALLOWED
)


@pytest.mark.parametrize(
    ("current", "target"),
    _SCHEDULE_ALLOWED,
    ids=[f"{current.value}->{target.value}" for current, target in _SCHEDULE_ALLOWED],
)
def test_schedule_transitions_allow_every_defined_edge(
    current: ScheduleStatus, target: ScheduleStatus
) -> None:
    assert require_schedule_transition(current, target) is None


@pytest.mark.parametrize(
    ("current", "target"),
    _SCHEDULE_REJECTED,
    ids=[f"{current.value}->{target.value}" for current, target in _SCHEDULE_REJECTED],
)
def test_schedule_transitions_reject_every_undefined_edge(
    current: ScheduleStatus, target: ScheduleStatus
) -> None:
    with pytest.raises(ConflictError) as exc_info:
        require_schedule_transition(current, target)

    assert exc_info.value.code == "invalid_task_schedule_transition"


@pytest.mark.parametrize(
    ("current", "target"),
    _EXECUTION_ALLOWED,
    ids=[f"{current.value}->{target.value}" for current, target in _EXECUTION_ALLOWED],
)
def test_execution_transitions_allow_every_defined_edge(
    current: ExecutionStatus, target: ExecutionStatus
) -> None:
    assert require_execution_transition(current, target) is None


@pytest.mark.parametrize(
    ("current", "target"),
    _EXECUTION_REJECTED,
    ids=[f"{current.value}->{target.value}" for current, target in _EXECUTION_REJECTED],
)
def test_execution_transitions_reject_every_undefined_edge(
    current: ExecutionStatus, target: ExecutionStatus
) -> None:
    with pytest.raises(ConflictError) as exc_info:
        require_execution_transition(current, target)

    assert exc_info.value.code == "invalid_execution_transition"


@pytest.mark.parametrize(
    ("current", "target"),
    _ATTEMPT_ALLOWED,
    ids=[f"{current.value}->{target.value}" for current, target in _ATTEMPT_ALLOWED],
)
def test_attempt_transitions_allow_every_defined_edge(
    current: AttemptStatus, target: AttemptStatus
) -> None:
    assert require_attempt_transition(current, target) is None


@pytest.mark.parametrize(
    ("current", "target"),
    _ATTEMPT_REJECTED,
    ids=[f"{current.value}->{target.value}" for current, target in _ATTEMPT_REJECTED],
)
def test_attempt_transitions_reject_every_undefined_edge(
    current: AttemptStatus, target: AttemptStatus
) -> None:
    with pytest.raises(ConflictError) as exc_info:
        require_attempt_transition(current, target)

    assert exc_info.value.code == "invalid_execution_attempt_transition"


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (ExecutionStatus.WAITING, False),
        (ExecutionStatus.QUEUED, False),
        (ExecutionStatus.RUNNING, False),
        (ExecutionStatus.ENDED, True),
        (ExecutionStatus.CANCELLED, True),
        (ExecutionStatus.TIMED_OUT, True),
    ),
    ids=[status.value for status in ExecutionStatus],
)
def test_is_execution_terminal_matches_each_execution_status(
    status: ExecutionStatus, expected: bool
) -> None:
    assert is_execution_terminal(status) is expected
