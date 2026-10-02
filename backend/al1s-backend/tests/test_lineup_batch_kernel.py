from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from al1s.execution.scheduling_helpers import _task_history_management
from al1s.execution.scheduling_types import TaskLifecycleStatus, TaskType
from al1s.execution.task_settlement import settle_task_after_execution


def test_batch_does_not_settle_parent_until_every_occurrence_ends():
    task = SimpleNamespace(
        task_id=uuid4(),
        task_type=TaskType.BATCH,
        lifecycle_status=TaskLifecycleStatus.ACTIVE,
        row_version=1,
    )
    uow = SimpleNamespace(tasks=Mock(), occurrences=Mock(), transitions=Mock())
    uow.tasks.get.return_value = task
    uow.occurrences.has_unsettled_for_task.return_value = True
    settle_task_after_execution(
        uow, task_id=task.task_id, schedule=None, correlation_id=uuid4(), now=datetime.now(UTC)
    )
    uow.tasks.set_lifecycle.assert_not_called()


def test_active_batch_remains_cancellable_between_images():
    item = SimpleNamespace(
        task_type=TaskType.BATCH,
        lifecycle_status=TaskLifecycleStatus.ACTIVE,
        latest_execution_status="ended",
    )
    assert _task_history_management(item).cancel.allowed
