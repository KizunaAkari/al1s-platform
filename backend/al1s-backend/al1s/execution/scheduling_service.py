"""Scheduling facade; operations retain their existing UnitOfWork boundaries."""

from al1s.execution.scheduling_base import SchedulingUowFactory as SchedulingUowFactory
from al1s.execution.scheduling_commands import SchedulingCommands
from al1s.execution.scheduling_materialization import ScheduleMaterialization
from al1s.execution.scheduling_queries import SchedulingQueries
from al1s.execution.scheduling_schedule_commands import ScheduleCommands


class ExecutionSchedulingService(
    SchedulingCommands, SchedulingQueries, ScheduleCommands, ScheduleMaterialization
):
    """Task commands, schedule commands, materialization and queries."""
