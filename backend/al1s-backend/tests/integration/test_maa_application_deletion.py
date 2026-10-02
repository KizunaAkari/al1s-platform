"""Bounded previews, bulk rollback and task-insert fencing in a disposable database."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.maa_application_deletion_repository import (
    PostgresMaaApplicationDeletionRepository,
)
from al1s.adapters.postgres.maa_models import (
    MaaApplicationRow,
    MaaScriptRow,
    MaaScriptVersionRow,
    MaaStrategyRow,
)
from al1s.adapters.postgres.maa_unit_of_work import MaaSqlAlchemyUnitOfWork
from al1s.adapters.postgres.scheduling_models import TaskRequestRow
from al1s.adapters.postgres.scheduling_repository_records import _task_record
from al1s.adapters.postgres.scheduling_task_repository import PostgresTaskRepository
from al1s.execution.errors import ConflictError
from al1s.maa.catalog_command_service import MaaCatalogCommandService
from al1s.maa.catalog_service import MaaCatalogQueryService
from al1s.maa.errors import MaaDomainError
from tests.integration.maa_support import clean_maa_tables as clean_maa_tables
from tests.integration.maa_support import engine as engine

pytestmark = pytest.mark.integration
NOW = datetime.now(UTC)


def application(session, package="com.test.delete"):
    row = MaaApplicationRow(
        id=uuid4(),
        package_name=package,
        display_name=package,
        row_version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(row)
    session.flush()
    return row.id


def script(session, app_id, name="Script", deleted=False):
    row = MaaScriptRow(
        id=uuid4(),
        application_id=app_id,
        name=name,
        normalized_name=name.lower(),
        script_type="module_process",
        status="validation_pending",
        row_version=1,
        created_at=NOW,
        updated_at=NOW,
        deleted_at=NOW if deleted else None,
    )
    session.add(row)
    session.flush()
    return row.id


def strategy(session, app_id, name="Strategy", deleted=False):
    row = MaaStrategyRow(
        id=uuid4(),
        application_id=app_id,
        name=name,
        normalized_name=name.lower(),
        status="active",
        row_version=1,
        created_at=NOW,
        updated_at=NOW,
        deleted_at=NOW if deleted else None,
    )
    session.add(row)
    session.flush()
    return row.id


def task(session, identity, *, status="active", deleted=False, source="maa", guarded=False):
    task_id = uuid4()
    row = TaskRequestRow(
        id=task_id,
        idempotency_key=str(task_id),
        request_hash="0" * 64,
        name="Test",
        task_type="single",
        lifecycle_status=status,
        source_module=source,
        logical_content_id=identity,
        parameters={},
        timeout_seconds=60,
        max_retries=0,
        record_video=False,
        deleted_at=NOW if deleted else None,
        created_at=NOW,
        row_version=1,
    )
    if guarded:
        PostgresTaskRepository(session).add(_task_record(row))
    else:
        session.add(row)
    session.flush()
    return task_id


def services(engine):
    sessions = sessionmaker(engine, expire_on_commit=False)

    def factory():
        return MaaSqlAlchemyUnitOfWork(sessions)

    return MaaCatalogQueryService(factory), MaaCatalogCommandService(factory)


def delete(commands, app_id, key="delete"):
    return commands.delete_application(
        application_id=app_id,
        expected_row_version=1,
        idempotency_key=key,
        correlation_id=uuid4(),
    )


def test_preview_counts_and_blockers_preserve_soft_delete_and_task_scope(engine):
    with Session(engine) as session, session.begin():
        app_id = application(session)
        script_id = script(session, app_id)
        removed_id = script(session, app_id, "Removed", deleted=True)
        strategy_id = strategy(session, app_id)
        strategy(session, app_id, "Removed", deleted=True)
        expected = [task(session, f"script:{script_id}") for _ in range(28)]
        expected += [task(session, f"strategy:{strategy_id}") for _ in range(27)]
        expected.append(task(session, f"script:{str(script_id).upper()}"))
        task(session, "script:old-invalid-format")
        task(session, "strategy:not-a-uuid")
        task(session, f"script:{removed_id}")
        task(session, f"script:{script_id}", status="completed")
        task(session, f"script:{script_id}", deleted=True)
        task(session, f"script:{script_id}", source="other")
    queries, commands = services(engine)
    preview = queries.application_delete_preview(app_id)
    assert (preview["script_count"], preview["strategy_count"], preview["device_count"]) == (
        1,
        1,
        0,
    )
    assert preview["blocking_task_ids"] == [str(value) for value in sorted(expected)[:50]]
    assert preview["has_more_blockers"] is True
    with pytest.raises(MaaDomainError) as blocked:
        delete(commands, app_id)
    assert blocked.value.code == "application_used_by_tasks"


def test_empty_preview_and_task_created_after_preview_is_rechecked(engine):
    with Session(engine) as session, session.begin():
        app_id = application(session)
    queries, commands = services(engine)
    empty = queries.application_delete_preview(app_id)
    assert empty["script_count"] == empty["strategy_count"] == 0
    with Session(engine) as session, session.begin():
        strategy_id = strategy(session, app_id)
    assert queries.application_delete_preview(app_id)["blocking_task_ids"] == []
    with Session(engine) as session, session.begin():
        task_id = task(session, f"strategy:{strategy_id}")
    with pytest.raises(MaaDomainError) as blocked:
        delete(commands, app_id)
    assert blocked.value.context["task_ids"] == [str(task_id)]
    with Session(engine) as session:
        assert session.get(MaaApplicationRow, app_id).deleted_at is None


def test_cascade_is_bulk_atomic_replayable_and_keeps_history(engine):
    with Session(engine) as session, session.begin():
        app_id = application(session)
        script_id = script(session, app_id)
        strategy_id = strategy(session, app_id)
        version_id = uuid4()
        session.add(
            MaaScriptVersionRow(
                id=version_id,
                script_id=script_id,
                revision=1,
                schema_version=2,
                manifest_hash="0" * 64,
                manifest={"steps": []},
                created_at=NOW,
            )
        )
        history_id = task(session, f"script:{script_id}", status="completed")
    _, commands = services(engine)
    first, replay = delete(commands, app_id), delete(commands, app_id)
    assert not first.replayed and replay.replayed
    with Session(engine) as session:
        assert session.get(MaaScriptRow, script_id).deleted_at is not None
        assert session.get(MaaStrategyRow, strategy_id).status == "retired"
        assert session.get(MaaStrategyRow, strategy_id).row_version == 2
        assert session.get(MaaScriptVersionRow, version_id) is not None
        assert session.get(TaskRequestRow, history_id).deleted_at is None


def test_failure_after_bulk_updates_rolls_back_everything(engine):
    with Session(engine) as session, session.begin():
        app_id = application(session)
        script_id = script(session, app_id)
    _, commands = services(engine)

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE maa_applications"):
            raise RuntimeError("simulated failure after cascade")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="after cascade"):
            delete(commands, app_id)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with Session(engine) as session:
        assert session.get(MaaScriptRow, script_id).deleted_at is None
        assert session.get(MaaApplicationRow, app_id).deleted_at is None
    assert not delete(commands, app_id).replayed


def test_external_effective_recovery_reference_blocks_cascade(engine):
    with Session(engine) as session, session.begin():
        app_id = application(session)
        target_id = script(session, app_id)
        source_app = application(session, "com.test.outside")
        source_id = script(session, source_app)
        version_id = uuid4()
        session.add(
            MaaScriptVersionRow(
                id=version_id,
                script_id=source_id,
                revision=1,
                schema_version=2,
                manifest_hash="0" * 64,
                created_at=NOW,
                manifest={
                    "steps": [
                        {
                            "action": "wait",
                            "failure_retry": {
                                "enabled": True,
                                "process_script_id": str(target_id),
                            },
                        }
                    ]
                },
            )
        )
        session.flush()
        session.execute(
            update(MaaScriptRow)
            .where(MaaScriptRow.id == source_id)
            .values(current_version_id=version_id)
        )
    _, commands = services(engine)
    with pytest.raises(MaaDomainError) as blocked:
        delete(commands, app_id)
    assert blocked.value.code == "application_external_reference"
    with Session(engine) as session:
        assert session.get(MaaScriptRow, target_id).deleted_at is None


def test_unused_historical_recovery_reference_does_not_block_cascade(engine):
    with Session(engine) as session, session.begin():
        app_id = application(session)
        target_id = script(session, app_id)
        source_id = script(session, application(session, "com.test.history"))
        version_id = uuid4()
        session.add(
            MaaScriptVersionRow(
                id=version_id,
                script_id=source_id,
                revision=1,
                schema_version=2,
                manifest_hash="0" * 64,
                created_at=NOW,
                manifest={
                    "steps": [
                        {
                            "failure_retry": {
                                "enabled": True,
                                "process_script_id": str(target_id),
                            }
                        }
                    ]
                },
            )
        )
    delete(services(engine)[1], app_id)
    with Session(engine) as session:
        assert session.get(MaaScriptVersionRow, version_id) is not None
        assert session.get(MaaScriptRow, target_id).deleted_at is not None


def test_task_committing_first_is_seen_by_waiting_delete_transaction(engine):
    with Session(engine) as session, session.begin():
        app_id = application(session)
        strategy_id = strategy(session, app_id)
    started, completed = Event(), Event()

    def remove():
        started.set()
        try:
            delete(services(engine)[1], app_id)
        finally:
            completed.set()

    with ThreadPoolExecutor(max_workers=1) as workers:
        with Session(engine) as session, session.begin():
            task_id = task(session, f"strategy:{strategy_id}", guarded=True)
            pending = workers.submit(remove)
            assert started.wait(2)
            assert not completed.wait(0.1)
        with pytest.raises(MaaDomainError) as blocked:
            pending.result(timeout=2)
    assert blocked.value.code == "application_used_by_tasks"
    assert blocked.value.context["task_ids"] == [str(task_id)]
    with Session(engine) as session:
        assert session.get(MaaApplicationRow, app_id).deleted_at is None


@pytest.mark.parametrize("kind", ["script", "strategy"])
@pytest.mark.parametrize("removed", [True, False])
def test_task_insert_rechecks_after_deletion_content_locks_release(engine, kind, removed):
    with Session(engine) as session, session.begin():
        app_id = application(session)
        content_id = script(session, app_id) if kind == "script" else strategy(session, app_id)
    started, committed = Event(), Event()

    def insert():
        with Session(engine) as session, session.begin():
            started.set()
            task(session, f"{kind}:{content_id}", guarded=True)
        committed.set()

    with ThreadPoolExecutor(max_workers=1) as workers:
        with Session(engine) as session, session.begin():
            repository = PostgresMaaApplicationDeletionRepository(session)
            repository.lock_contents(app_id)
            pending = workers.submit(insert)
            assert started.wait(2)
            assert not committed.wait(0.1)
            assert repository.active_tasks(app_id) == []
            if removed:
                repository.soft_delete_contents(app_id, NOW)
        if removed:
            with pytest.raises(ConflictError, match="任务内容"):
                pending.result(timeout=2)
        else:
            pending.result(timeout=2)
    assert committed.is_set() is (not removed)
    assert len(services(engine)[0].application_delete_preview(app_id)["blocking_task_ids"]) == (
        0 if removed else 1
    )


@pytest.mark.parametrize("size", [1000, 100000])
def test_large_preview_uses_three_queries_and_no_unbounded_parameter_list(engine, size):
    with Session(engine) as session, session.begin():
        app_id = application(session)
        # Generate representative identities in PostgreSQL, not a huge Python ORM list.
        session.execute(
            text("""
            INSERT INTO maa_scripts
                (id, application_id, name, normalized_name, script_type, status,
                 created_at, updated_at, row_version)
            SELECT gen_random_uuid(), :application_id, 'script-' || n, 'script-' || n,
                   'module_process', 'validation_pending', now(), now(), 1
            FROM generate_series(1, :size) n
        """),
            {"application_id": app_id, "size": size},
        )
    statements = []

    def count(connection, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", count)
    try:
        preview = services(engine)[0].application_delete_preview(app_id)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert preview["script_count"] == size and preview["blocking_task_ids"] == []
    assert len(statements) == 3
    assert max(len(parameters) for _, parameters in statements) < 20
    assert (
        len(services(engine)[0].list_scripts(application_id=app_id, after_id=None, limit=50)) == 50
    )
    if size == 100000:
        assert not delete(services(engine)[1], app_id).replayed
        with Session(engine) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(MaaScriptRow)
                    .where(MaaScriptRow.deleted_at.is_(None))
                )
                == 0
            )
