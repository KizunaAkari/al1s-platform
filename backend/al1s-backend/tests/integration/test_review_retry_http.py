import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.delivery_models import TaskPackageRow as Package
from al1s.adapters.postgres.lineup_batch_commands import LineupBatchCommands
from al1s.adapters.postgres.lineup_models import LineupRecognitionRow as Record
from al1s.adapters.postgres.lineup_repository import LineupRepository
from al1s.adapters.postgres.scheduling_models import TaskRequestRow as Task
from al1s.adapters.postgres.scheduling_unit_of_work import SchedulingSqlAlchemyUnitOfWork
from al1s.app.config import Settings
from al1s.app.factory import create_app
from al1s.execution.definitions import ExecutionDefinitionRegistry
from al1s.execution.scheduling_service import ExecutionSchedulingService
from al1s.execution.scheduling_types import CreateTaskCommand, ExecutionResult, TaskType
from al1s.infrastructure.readiness import ReadinessResult
from al1s.lineup.definition import LineupDefinitionProvider
from tests.integration.execution_support import clean_tables as clean_tables
from tests.integration.execution_support import engine as engine
from tests.integration.test_lineup_workspace import setup

pytestmark = pytest.mark.integration


def _row_counts(engine: Engine) -> tuple[int, int, int]:
    with Session(engine) as session:
        return tuple(
            int(session.scalar(select(func.count()).select_from(row)) or 0)
            for row in (Task, Package, Record)
        )


def _materialize_and_fail(scheduling: ExecutionSchedulingService) -> UUID:
    items = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())
    assert len(items) == 1
    item = items[0]
    scheduling.start_attempt(item.attempt.attempt_id, correlation_id=uuid4())
    scheduling.complete_attempt(
        item.attempt.attempt_id,
        result=ExecutionResult.FAILURE,
        error_code="failed",
        retryable=False,
        correlation_id=uuid4(),
    )
    return item.execution.execution_id


def test_lineup_executions_reject_generic_snapshot_retry_without_creating_rows(
    engine: Engine,
) -> None:
    sessions, terminal, record_ids, batch_scheduling = setup(engine, 1)
    LineupBatchCommands(sessions).create(record_ids, terminal, {}, "review-cr007-batch")
    batch_execution_id = _materialize_and_fail(batch_scheduling)

    definitions = ExecutionDefinitionRegistry()
    definitions.register("lineup", LineupDefinitionProvider(LineupRepository(sessions)))
    single_scheduling = ExecutionSchedulingService(
        lambda: SchedulingSqlAlchemyUnitOfWork(sessions), definitions
    )
    single_scheduling.create_task(
        CreateTaskCommand(
            idempotency_key="review-cr007-single",
            name="legacy lineup single",
            task_type=TaskType.SINGLE,
            source_module="lineup",
            logical_content_id=str(record_ids[0]),
            parameters={},
            requested_terminal_id=terminal,
            requested_target_device_id=None,
            timeout_seconds=30,
            max_retries=0,
            record_video=False,
        ),
        correlation_id=uuid4(),
    )
    single_execution_id = _materialize_and_fail(single_scheduling)

    class Ready:
        def check(self) -> ReadinessResult:
            return ReadinessResult(ready=True, dependencies={})

        def close(self) -> None:
            return

    app = create_app(
        Settings(
            database_url=engine.url.render_as_string(hide_password=False),
            admin_password="review-test-password",
            admin_cookie_secure=False,
        ),
        readiness_service=Ready(),  # type: ignore[arg-type]
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                headers={"X-AL1S-CSRF": "1"},
            ) as client,
        ):
            logged_in = await client.post(
                "/api/v1/auth/login",
                json={"password": "review-test-password"},
            )
            assert logged_in.status_code == 200, logged_in.text
            before_requests = _row_counts(engine)
            for execution_id, key in (
                (batch_execution_id, "review-cr007-batch-retry"),
                (single_execution_id, "review-cr007-single-retry"),
            ):
                for _ in range(2):
                    response = await client.post(
                        f"/api/v1/executions/{execution_id}/retry-original-snapshot",
                        json={"idempotency_key": key},
                    )
                    assert response.status_code == 409, response.text
                    assert response.json()["code"] == "lineup_retry_requires_workspace"
                    assert _row_counts(engine) == before_requests

    asyncio.run(exercise())
