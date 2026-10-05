import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, insert, text
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.component_repository import ComponentRepository
from al1s.execution.errors import ConflictError
from tests.test_component_inventory import release


@pytest.fixture
def repository():
    url = os.environ.get("AL1S_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated component PostgreSQL required")
    engine = create_engine(url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_database()")) == "component_test"
        connection.rollback()
        transaction = connection.begin()
        sessions = sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield ComponentRepository(sessions)
        finally:
            transaction.rollback()
    engine.dispose()


def test_release_identity_replay_and_changed_payload_are_explicit(repository):
    body, now = release(), datetime.now(UTC)
    first = repository.create_release(body, now)
    assert repository.create_release(body, now) == first
    with pytest.raises(ConflictError):
        repository.create_release(body.model_copy(update={"size_bytes": 124}), now)
    page = repository.list_releases("postgresql", 25, None)
    assert first in page.items and len(page.items) <= 25


def test_inventory_empty_query_is_one_bounded_join(repository):
    rows, cursor = repository.list_linux_profiles(20, None)
    assert rows == [] and cursor is None


def test_large_terminal_inventory_uses_one_query_per_page(repository):
    from uuid import uuid4

    from al1s.adapters.postgres.execution_models import TerminalRow

    with repository.sessions.begin() as session:
        session.execute(
            insert(TerminalRow),
            [
                {
                    "id": uuid4(),
                    "installation_id": uuid4(),
                    "terminal_type": "linux",
                    "display_name": "synthetic",
                    "agent_version": "agent",
                    "service_status": "offline",
                    "acceptance_status": "accepting",
                    "row_version": 1,
                    "name_version": 0,
                }
                for _ in range(2000)
            ],
        )
    calls = []
    connection = repository.sessions.kw["bind"]

    def count(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            calls.append(1)

    event.listen(connection, "before_cursor_execute", count)
    try:
        first, cursor = repository.list_linux_profiles(20, None)
        second, following = repository.list_linux_profiles(20, cursor)
        assert len(first) == len(second) == 20 and cursor and following
        assert set(item.terminal_id for item in first).isdisjoint(
            item.terminal_id for item in second
        )
        assert len(calls) == 2
    finally:
        event.remove(connection, "before_cursor_execute", count)
