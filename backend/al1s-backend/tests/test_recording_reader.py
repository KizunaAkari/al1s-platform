"""Execute the projection on a minimal SQLite schema, not a migration test."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Uuid,
    create_engine,
    event,
)
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.recording_reader import PostgresRecordingReader


@pytest.mark.parametrize(
    "scenario",
    [
        "allowed",
        "cancelled",
        "wrong_task",
        "deleted",
        "not_recorded",
        "boundary",
        "before_boundary",
        "not_ended",
        "running",
        "pending",
        "blob_deleted",
        "screenshot",
    ],
)
def test_download_projection_filters_and_single_query(scenario):
    metadata = MetaData()
    tables = {}
    schema = {
        "task_requests": {"id": Uuid, "deleted_at": DateTime, "record_video": Boolean},
        "executions": {"id": Uuid, "task_request_id": Uuid},
        "execution_attempts": {
            "id": Uuid,
            "execution_id": Uuid,
            "status": String,
            "ended_at": DateTime,
        },
        "terminal_artifacts": {
            "id": Uuid,
            "owner_id": Uuid,
            "blob_id": Uuid,
            "owner_kind": String,
            "artifact_kind": String,
            "status": String,
            "file_name": String,
        },
        "blob_objects": {
            "id": Uuid,
            "sha256": String,
            "size_bytes": Integer,
            "object_key": String,
            "status": String,
            "media_type": String,
        },
    }
    for name, fields in schema.items():
        tables[name] = Table(
            name,
            metadata,
            *[Column(field, kind, primary_key=field == "id") for field, kind in fields.items()],
        )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    task, execution, attempt, artifact, blob = [uuid4() for _ in range(5)]
    now = datetime(2026, 9, 8, tzinfo=UTC)
    ended = now - timedelta(days=1)
    if scenario == "boundary":
        ended = now - timedelta(days=30)
    if scenario == "before_boundary":
        ended = now - timedelta(days=30) + timedelta(seconds=1)
    rows = {
        "task_requests": dict(
            id=task,
            deleted_at=now if scenario == "deleted" else None,
            record_video=scenario != "not_recorded",
        ),
        "executions": dict(id=execution, task_request_id=task),
        "execution_attempts": dict(
            id=attempt,
            execution_id=execution,
            status=scenario if scenario in ("running", "cancelled") else "ended",
            ended_at=None if scenario == "not_ended" else ended,
        ),
        "terminal_artifacts": dict(
            id=artifact,
            owner_id=attempt,
            blob_id=blob,
            owner_kind="formal_attempt",
            artifact_kind="screenshot" if scenario == "screenshot" else "video",
            status="pending" if scenario == "pending" else "ready",
            file_name="recording.mp4",
        ),
        "blob_objects": dict(
            id=blob,
            sha256="a" * 64,
            size_bytes=8,
            object_key="private/key",
            status="deleted" if scenario == "blob_deleted" else "ready",
            media_type="video/mp4",
        ),
    }
    with engine.begin() as connection:
        for name, values in rows.items():
            connection.execute(tables[name].insert().values(**values))
    calls = []
    event.listen(engine, "before_cursor_execute", lambda *args: calls.append(args[2]))
    result = PostgresRecordingReader(sessionmaker(engine)).find_downloadable(
        uuid4() if scenario == "wrong_task" else task, artifact, now
    )
    assert (result is not None) == (scenario in ("allowed", "cancelled", "before_boundary"))
    assert len(calls) == 1
    calls.clear()
    page = PostgresRecordingReader(sessionmaker(engine)).list_recordings(
        uuid4() if scenario == "wrong_task" else task, now, None, 20
    )
    visible = scenario not in ("wrong_task", "deleted", "not_recorded", "screenshot")
    assert len(page.items) == int(visible)
    if visible:
        assert page.items[0].downloadable == (result is not None)
        assert page.items[0].artifact_id == artifact
    assert page.next_cursor is None
    assert len(calls) == 1
    assert (
        not PostgresRecordingReader(sessionmaker(engine))
        .list_recordings(task, now, artifact, 20)
        .items
    )
    if scenario == "allowed":
        with engine.begin() as connection:
            connection.execute(
                tables["terminal_artifacts"].insert(),
                [{**rows["terminal_artifacts"], "id": uuid4()} for _ in range(100)],
            )
        reader = PostgresRecordingReader(sessionmaker(engine))
        seen = []
        cursor = None
        while True:
            calls.clear()
            page = reader.list_recordings(task, now, cursor, 20)
            assert len(calls) == 1 and len(page.items) <= 20
            seen.extend(item.artifact_id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 101
        assert seen == sorted(seen)
    engine.dispose()
