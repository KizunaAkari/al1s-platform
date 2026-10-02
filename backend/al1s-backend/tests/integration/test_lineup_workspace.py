from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.lineup_annotation_repository import LineupAnnotationRepository
from al1s.adapters.postgres.lineup_batch_commands import LineupBatchCommands
from al1s.adapters.postgres.lineup_models import LineupRecognitionRow as Record
from al1s.adapters.postgres.lineup_repository import LineupRepository
from al1s.adapters.postgres.lineup_workspace_queries import LineupWorkspaceQueries
from al1s.adapters.postgres.models import BlobObjectRow as Blob
from al1s.adapters.postgres.scheduling_models import TaskRequestRow as Task
from al1s.adapters.postgres.scheduling_unit_of_work import SchedulingSqlAlchemyUnitOfWork
from al1s.execution.definitions import ExecutionDefinitionRegistry
from al1s.execution.errors import ConflictError
from al1s.execution.scheduling_service import ExecutionSchedulingService
from al1s.execution.scheduling_types import ExecutionResult
from al1s.lineup.batch_definition import LineupBatchDefinitionProvider
from al1s.lineup.catalog import catalog
from tests.integration.execution_support import _prepare_terminal, _services
from tests.integration.execution_support import clean_tables as clean_tables
from tests.integration.execution_support import engine as engine

pytestmark = pytest.mark.integration


def test_actual_scheduler_composition_dispatches_lineup_batches(engine):
    from al1s.adapters.postgres.scheduling_models import ExecutionRow
    from al1s.infrastructure.scheduler_worker import build_runtime_worker

    sessions, terminal, ids, _ = setup(engine, 2)
    task_id = LineupBatchCommands(sessions).create(ids, terminal, {}, "real-scheduler")
    build_runtime_worker(sessions).run_once()
    with sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionRow)
                .where(ExecutionRow.task_request_id == task_id)
            )
            == 1
        )


def setup(engine, count=3):
    now = datetime.now(UTC)
    resource, _ = _services(engine, [now])
    terminal, _ = _prepare_terminal(resource)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as s:
        from al1s.adapters.postgres.execution_models import TerminalCapabilityProfileRow

        profiles = list(s.scalars(select(TerminalCapabilityProfileRow)))
        for profile in profiles:
            profile.provider_keys = ["maa", "lineup-recognition-v1"]
        blob = Blob(
            id=uuid4(),
            sha256=uuid4().hex * 2,
            size_bytes=1,
            media_type="image/png",
            object_key=str(uuid4()),
            status="ready",
            ready_at=now,
        )
        s.add(blob)
        s.flush()
        records = [
            Record(
                id=uuid4(),
                blob_id=blob.id,
                name=f"image-{i}.png",
                width=100,
                height=100,
                catalog_version=catalog()["version"],
                created_at=now,
                row_version=1,
            )
            for i in range(count)
        ]
        s.add_all(records)
    repo = LineupRepository(sessions)
    definitions = ExecutionDefinitionRegistry()
    definitions.register("lineup_batch", LineupBatchDefinitionProvider(repo))
    scheduling = ExecutionSchedulingService(
        lambda: SchedulingSqlAlchemyUnitOfWork(sessions), definitions
    )
    return sessions, terminal, [r.id for r in records], scheduling


def test_batch_is_one_task_and_advances_after_each_failure_then_settles(engine):
    sessions, terminal, ids, scheduling = setup(engine)
    commands = LineupBatchCommands(sessions)
    task_id = commands.create(ids, terminal, {}, "batch-test")
    assert commands.create(ids, terminal, {}, "batch-test") == task_id
    with pytest.raises(ConflictError):
        commands.create(list(reversed(ids)), terminal, {}, "batch-test")
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 1
    for index in range(3):
        items = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())
        assert len(items) == 1 and items[0].occurrence.ordinal == index + 1
        assert scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4()) == []
        scheduling.start_attempt(items[0].attempt.attempt_id, correlation_id=uuid4())
        scheduling.complete_attempt(
            items[0].attempt.attempt_id,
            result=ExecutionResult.FAILURE,
            error_code="lineup_test_failure",
            retryable=False,
            correlation_id=uuid4(),
        )
        with Session(engine) as session:
            assert session.get(Task, task_id).lifecycle_status == (
                "completed" if index == 2 else "active"
            )
    page = LineupWorkspaceQueries(sessions).task(task_id)
    assert [item["id"] for item in page["items"]] == ids
    assert page["summary"]["failure"] == page["summary"]["needs_attention"] == 3


def test_failed_annotation_cas_and_retry_skips_manually_resolved_images(engine):
    sessions, terminal, ids, scheduling = setup(engine, 2)
    commands = LineupBatchCommands(sessions)
    original = commands.create(ids, terminal, {}, "original")
    for _ in ids:
        item = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
        scheduling.start_attempt(item.attempt.attempt_id, correlation_id=uuid4())
        scheduling.complete_attempt(
            item.attempt.attempt_id,
            result=ExecutionResult.FAILURE,
            error_code="failed",
            retryable=False,
            correlation_id=uuid4(),
        )
    annotation = {
        "teams": {"attack": 1},
        "slots": [
            {
                "side": "attack",
                "index": 0,
                "student_id": catalog()["students"][0]["id"],
                "regions": [{"kind": "portrait", "box": [0, 0, 20, 20]}],
            }
        ],
    }
    saved = LineupAnnotationRepository(sessions).save(ids[0], 0, annotation)
    assert saved["state"] == "confirmed" and saved["version"] == 1
    with pytest.raises(ConflictError):
        LineupAnnotationRepository(sessions).save(ids[0], 0, annotation)
    retry = commands.create([], terminal, {}, "retry", retry_source=original)
    assert commands.create([], terminal, {}, "other-tab", retry_source=original) == retry

    result = LineupWorkspaceQueries(sessions).task(retry)
    assert len(result["items"]) == 1 and result["items"][0]["retry_source_record_id"] == ids[1]
    old = LineupWorkspaceQueries(sessions).task(original)
    assert old["summary"]["usable"] == 1 and old["summary"]["needs_attention"] == 1
    with sessions.begin() as session:
        session.get(Task, retry).lifecycle_status = "completed"
    # A second tab received the active retry. Replaying that same request after
    # completion must still return it, not silently create another retry.
    assert commands.create([], terminal, {}, "other-tab", retry_source=original) == retry


def test_two_concurrent_submissions_create_only_one_owned_task(engine):
    from concurrent.futures import ThreadPoolExecutor

    sessions, terminal, ids, _ = setup(engine, 4)

    def submit(_):
        return LineupBatchCommands(sessions).create(ids, terminal, {}, "same-click")

    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(submit, range(2)))
    assert result[0] == result[1]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 1


def test_cancel_between_images_cancels_remaining_without_rewriting_failed_image(engine):
    sessions, terminal, ids, scheduling = setup(engine)
    task_id = LineupBatchCommands(sessions).create(ids, terminal, {}, "cancel-batch")
    item = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
    scheduling.start_attempt(item.attempt.attempt_id, correlation_id=uuid4())
    scheduling.complete_attempt(
        item.attempt.attempt_id,
        result=ExecutionResult.FAILURE,
        error_code="failed",
        retryable=False,
        correlation_id=uuid4(),
    )
    task = scheduling.get_task(task_id).task
    scheduling.cancel_single_task(
        task_id, expected_version=task.row_version, correlation_id=uuid4()
    )
    assert scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4()) == []
    page = LineupWorkspaceQueries(sessions).task(task_id)
    assert [i["state"] for i in page["items"]] == ["failure", "cancelled", "cancelled"]
    assert page["summary"]["finished"] == 3


def test_task_image_page_has_fixed_query_count_at_two_hundred_images(engine):
    from sqlalchemy import event, text

    sessions, terminal, ids, _ = setup(engine, 10_000)
    ids = ids[:200]
    task_id = LineupBatchCommands(sessions).create(ids, terminal, {}, "large-batch")
    calls = []

    def count(*args):
        calls.append(args[2])

    event.listen(engine, "before_cursor_execute", count)
    try:
        page = LineupWorkspaceQueries(sessions).task(task_id)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(calls) == 4
    assert len(page["items"]) == 20 and page["next_cursor"] == 20
    assert page["summary"]["total"] == 200
    with engine.begin() as connection:
        connection.execute(text("ANALYZE lineup_recognitions"))
        plan = connection.execute(
            text("EXPLAIN (FORMAT JSON) SELECT id FROM lineup_recognitions WHERE task_id=:id"),
            {"id": task_id},
        ).scalar()
    import json

    assert "ix_lineup_task_attention" in json.dumps(plan)


def test_http_upload_single_parent_annotation_ownership_and_export(engine):
    import asyncio
    import json
    import os
    from io import BytesIO
    from zipfile import ZipFile

    import httpx
    from PIL import Image

    from al1s.app.config import Settings
    from al1s.app.factory import create_app
    from al1s.infrastructure.readiness import ReadinessResult

    _, terminal, _, _ = setup(engine, 0)
    objects = {}

    class Store:
        def put(self, key, body, media_type):
            objects[key] = body

        def get_range(self, key, start, end):
            return objects[key][start : end + 1]

    class Ready:
        def check(self):
            return ReadinessResult(ready=True, dependencies={})

        def close(self):
            pass

    app = create_app(
        Settings(
            database_url=os.environ["AL1S_TEST_DATABASE_URL"],
            admin_password="workspace-test-password",
            admin_cookie_secure=False,
        ),
        readiness_service=Ready(),
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            app.state.lineup.images.store = Store()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"X-AL1S-CSRF": "1"},
            ) as client:
                assert (await client.get("/api/v1/lineup/annotation-tasks")).status_code == 401
                assert (
                    await client.post(
                        "/api/v1/auth/login", json={"password": "workspace-test-password"}
                    )
                ).status_code == 200
                output = BytesIO()
                Image.new("RGB", (64, 64), uuid4().int & 0xFFFFFF).save(output, "PNG")
                created = await client.post(
                    "/api/v1/lineup/records?name=api-test.png",
                    content=output.getvalue(),
                    headers={"Content-Type": "image/png"},
                )
                assert created.status_code == 201, created.text
                record = created.json()
                body = dict(
                    record_ids=[record["id"]],
                    terminal_id=str(terminal),
                    idempotency_key=str(uuid4()),
                )
                response = await client.post("/api/v1/lineup/tasks", json=body)
                assert response.status_code == 201, response.text
                task_id = response.json()["task_id"]
                assert (await client.post("/api/v1/lineup/tasks", json=body)).json()[
                    "task_id"
                ] == task_id
                detail = (await client.get(f"/api/v1/lineup/tasks/{task_id}")).json()
                assert detail["summary"]["total"] == 1 and detail["items"][0]["id"] == record["id"]
                base = f"/api/v1/lineup/records/{record['id']}/annotation"
                assert (await client.get(base, params={"task_id": str(uuid4())})).status_code == 400
                annotation = dict(
                    expected_version=0,
                    teams={"attack": 1},
                    slots=[
                        dict(
                            side="attack",
                            index=0,
                            student_id=catalog()["students"][0]["id"],
                            regions=[dict(kind="portrait", box=[0, 0, 20, 20])],
                        )
                    ],
                )
                response = await client.put(base, json=annotation)
                assert response.status_code == 200, response.text
                assert response.json()["annotation"]["state"] == "confirmed"
                assert (await client.put(base, json=annotation)).status_code == 409
                exported = await client.get(base + "/export")
                assert exported.status_code == 200
                with ZipFile(BytesIO(exported.content)) as archive:
                    assert set(archive.namelist()) == {"image.png", "annotation.json"}
                    assert (
                        json.loads(archive.read("annotation.json"))["annotation"]["slots"][0][
                            "student_id"
                        ]
                        == annotation["slots"][0]["student_id"]
                    )
                metadata = (await client.get(f"/api/v1/tasks/{task_id}/details")).json()
                assert metadata["source_module"] == "lineup_batch" and metadata["items"] == []

    asyncio.run(exercise())


def test_annotation_task_cursor_pages_without_duplicates(engine):
    sessions, terminal, ids, scheduling = setup(engine, 22)
    commands = LineupBatchCommands(sessions)
    keys = {commands.create([record], terminal, {}, f"selector-{i}")
            for i, record in enumerate(ids)}
    for item in scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4()):
        scheduling.start_attempt(item.attempt.attempt_id, correlation_id=uuid4())
        scheduling.complete_attempt(item.attempt.attempt_id, result=ExecutionResult.FAILURE,
                                    error_code="failed", retryable=False, correlation_id=uuid4())
    queries = LineupWorkspaceQueries(sessions)
    first = queries.annotation_tasks()
    second = queries.annotation_tasks(first["next_cursor"])
    assert len(first["items"]) == 20 and len(second["items"]) == 2
    assert {r["task_id"] for r in first["items"] + second["items"]} == keys
    assert second["next_cursor"] is None


def test_migration_refuses_to_discard_new_batch_data(engine, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from al1s.app.config import get_settings
    sessions, terminal, ids, _ = setup(engine, 1)
    task_id = LineupBatchCommands(sessions).create(ids, terminal, {}, "keep-data")
    assert engine.url.database == "lineup_test" and engine.url.host in {"127.0.0.1", "lineup-db"}
    monkeypatch.setenv("AL1S_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="Cannot discard saved lineup"):
            command.downgrade(Config("alembic.ini"), "20260929_0050")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
                == "20261001_0051"
            )
        with sessions() as session:
            assert session.get(Task, task_id) is not None
    finally:
        get_settings.cache_clear()
