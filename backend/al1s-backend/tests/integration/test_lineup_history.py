from uuid import uuid4

import pytest
from sqlalchemy import event

from al1s.adapters.postgres.lineup_batch_commands import LineupBatchCommands
from al1s.adapters.postgres.lineup_workspace_queries import LineupWorkspaceQueries
from al1s.adapters.postgres.scheduling_models import TaskRequestRow
from al1s.adapters.postgres.scheduling_task_repository import PostgresTaskRepository
from al1s.api.tasks import _history_response
from al1s.execution.scheduling_helpers import _task_history_management
from al1s.execution.scheduling_types import ExecutionResult
from tests.integration.execution_support import clean_tables as clean_tables
from tests.integration.execution_support import engine as engine
from tests.integration.test_lineup_workspace import setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("first_result", [ExecutionResult.SUCCESS, ExecutionResult.FAILURE])
def test_batch_history_aggregates_all_images_in_two_queries(engine, first_result):
    sessions, terminal, ids, scheduling = setup(engine, 2)
    task_id = LineupBatchCommands(sessions).create(ids, terminal, {}, "history")
    with sessions.begin() as session:
        row = session.get(TaskRequestRow, task_id)
        assert row.name == "阵容识别 · 2 张"
        row.name = "阵容识别 · Screenshot_very_long_legacy_name.jpg"

    def history():
        calls = []

        def counted(*args):
            calls.append(args[2])

        event.listen(engine, "before_cursor_execute", counted)
        try:
            with sessions() as session:
                rows = PostgresTaskRepository(session).list_history(
                    before_created_at=None, before_id=None, limit=30
                )
            assert len(calls) == 2
            return rows[0]
        finally:
            event.remove(engine, "before_cursor_execute", counted)

    assert history().batch_summary.execution_status == "waiting"
    for index, result in enumerate([first_result, ExecutionResult.SUCCESS]):
        item = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
        assert history().batch_summary.execution_status == "waiting"
        scheduling.start_attempt(item.attempt.attempt_id, correlation_id=uuid4())
        assert history().batch_summary.execution_status == "running"
        scheduling.complete_attempt(
            item.attempt.attempt_id,
            result=result,
            error_code="test" if result is ExecutionResult.FAILURE else None,
            retryable=False,
            correlation_id=uuid4(),
        )
        if index == 0:
            assert history().batch_summary.execution_status == "waiting"
            assert history().batch_summary.result is None
    row = history()
    assert row.name == "阵容识别 · 2 张"
    assert row.latest_result == ExecutionResult.SUCCESS
    assert row.batch_summary.result == first_result
    assert row.batch_summary.execution_status == "ended"
    payload = _history_response(_task_history_management(row)).model_dump(mode="json")
    assert payload["batch_summary"]["result"] == first_result
    assert LineupWorkspaceQueries(sessions).task(task_id)["name"] == row.name
    with sessions() as session:
        assert session.get(TaskRequestRow, task_id).name.endswith("legacy_name.jpg")
