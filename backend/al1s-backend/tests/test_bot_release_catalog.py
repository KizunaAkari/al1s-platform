from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event

from al1s.components.bot_contracts import QqCandidateDraft
from al1s.execution.errors import NotFoundError
from tests.test_component_postgres import repository as repository


def draft(version):
    return QqCandidateDraft(
        native_version=version,
        architecture="amd64",
        registry_digest="sha256:" + "a" * 64,
        platform_manifest_digest="sha256:" + "b" * 64,
        runtime_artifact_digest="sha256:" + "c" * 64,
        runtime_version="1.0.0",
    )


def test_bot_catalog_orders_numeric_versions_globally_and_pages_without_gaps(repository):
    now = datetime.now(UTC)
    repository.store_qq_candidates([draft("8.3.2"), draft("8.1.10"), draft("8.1.9")], now)
    for offset in range(2):
        repository.store_qq_candidates(
            [draft(f"8.0.{i + offset * 20}") for i in range(20)],
            now + timedelta(minutes=offset + 1),
        )
    calls = []

    def count(*args):
        if args[2].lstrip().upper().startswith("SELECT"):
            calls.append(args[2])

    engine = repository.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", count)
    try:
        first = repository.list_bot_releases("qq", 5, None)
        assert [row.native_version for row in first.items] == [
            "8.3.2",
            "8.1.10",
            "8.1.9",
            "8.0.39",
            "8.0.38",
        ]
        assert len(calls) == 1 and first.next_cursor is not None
        all_rows = first.items[:]
        cursor = first.next_cursor
        while cursor:
            calls.clear()
            page = repository.list_bot_releases("qq", 25, cursor)
            assert len(page.items) <= 25 and len(calls) == 2
            all_rows.extend(page.items)
            cursor = page.next_cursor
        assert len(all_rows) == 43 and len({row.release_id for row in all_rows}) == 43
        with pytest.raises(NotFoundError):
            repository.list_bot_releases("discord", 25, first.next_cursor)
    finally:
        event.remove(engine, "before_cursor_execute", count)


def test_hundredfold_catalog_keeps_reads_bounded_and_uses_database_top_n(repository):
    now = datetime.now(UTC)
    for offset in range(0, 1300, 20):
        repository.store_qq_candidates([draft(f"8.0.{i}") for i in range(offset, offset + 20)], now)
    observed = []

    def capture(_connection, _cursor, sql, parameters, *_):
        if sql.lstrip().upper().startswith("SELECT"):
            observed.append((sql, parameters))

    connection = repository.sessions.kw["bind"]
    event.listen(connection, "before_cursor_execute", capture)
    try:
        page = repository.list_bot_releases("qq", 5, None)
        assert [row.native_version for row in page.items] == [
            f"8.0.{i}" for i in range(1299, 1294, -1)
        ]
        assert len(observed) == 1
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    sql, parameters = observed[0]
    plan = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE, FORMAT JSON) " + sql, parameters
    ).scalar_one()[0]["Plan"]
    assert plan["Node Type"] == "Limit" and plan["Actual Rows"] == 6
    assert "top-N heapsort" in str(plan)
