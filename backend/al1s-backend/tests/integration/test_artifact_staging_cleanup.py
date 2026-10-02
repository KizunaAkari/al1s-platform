import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.artifact_models import TerminalArtifactRow
from al1s.adapters.postgres.artifact_staging_cleanup import PostgresArtifactStagingCleanup
from al1s.adapters.postgres.execution_models import TerminalRow
from al1s.adapters.postgres.models import BlobObjectRow
from al1s.maa.artifact_staging_cleanup import StagingSettlement

pytestmark = pytest.mark.integration


def test_due_rows_are_bounded_leased_and_pending_is_protected():
    url = os.getenv("AL1S_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AL1S_TEST_DATABASE_URL is required")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            outer = connection.begin()
            sessions = sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            now = datetime.now(UTC)
            terminal = uuid4()
            with sessions.begin() as session:
                session.add(TerminalRow(
                    id=terminal, installation_id=uuid4(), terminal_type="linux",
                    display_name="staging test", agent_version="test",
                ))
                session.flush()
                for status in ("pending", "expired", "ready"):
                    artifact = uuid4()
                    blob_id = uuid4() if status == "ready" else None
                    if blob_id is not None:
                        session.add(BlobObjectRow(
                            id=blob_id, sha256="b" * 64, size_bytes=1,
                            media_type="image/png", object_key=f"verified-artifacts/{blob_id}",
                            status="ready", ready_at=now - timedelta(days=2),
                        ))
                        session.flush()
                    session.add(TerminalArtifactRow(
                        id=artifact, terminal_id=terminal, owner_kind="quick_test",
                        owner_id=uuid4(), artifact_kind="screenshot", file_name="test.png",
                        expected_sha256="a" * 64, expected_size_bytes=1,
                        media_type="image/png", object_key=f"artifacts/{terminal}/{artifact}",
                        status=status, idempotency_key=str(artifact),
                        expires_at=now - timedelta(days=2),
                        completed_at=None if status == "pending" else now - timedelta(days=2),
                        blob_id=blob_id,
                        created_at=now - timedelta(days=2),
                        staging_cleanup_at=now - timedelta(minutes=1),
                    ))
            repository = PostgresArtifactStagingCleanup(sessions)
            claimed = repository.claim(now, limit=2, lease=timedelta(minutes=15))
            assert len(claimed) == 2
            assert claimed[0].artifact_id != claimed[1].artifact_id
            assert repository.claim(now, limit=1, lease=timedelta(minutes=15)) == ()
            outcomes = (
                StagingSettlement(claimed[0], None),
                StagingSettlement(claimed[1], now + timedelta(days=1)),
            )
            assert repository.settle_many(outcomes) == {
                claimed[0].artifact_id, claimed[1].artifact_id,
            }
            assert repository.settle_many(outcomes) == set()
            with sessions() as session:
                pending = session.scalar(select(TerminalArtifactRow).where(
                    TerminalArtifactRow.status == "pending"
                ))
                assert pending is not None and pending.staging_cleanup_at is not None
            outer.rollback()
    finally:
        engine.dispose()


def test_three_hundred_due_rows_still_use_one_batched_settlement():
    url = os.getenv("AL1S_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AL1S_TEST_DATABASE_URL is required")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            outer = connection.begin()
            sessions = sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            now = datetime.now(UTC)
            terminal = uuid4()
            with sessions.begin() as session:
                session.add(TerminalRow(
                    id=terminal, installation_id=uuid4(), terminal_type="linux",
                    display_name="staging bulk", agent_version="test",
                ))
                session.flush()
                session.add_all([
                    TerminalArtifactRow(
                        id=(artifact := uuid4()), terminal_id=terminal,
                        owner_kind="quick_test", owner_id=uuid4(),
                        artifact_kind="screenshot", file_name="bulk.png",
                        expected_sha256="a" * 64, expected_size_bytes=1,
                        media_type="image/png", object_key=f"artifacts/{terminal}/{artifact}",
                        status="expired", idempotency_key=str(artifact),
                        expires_at=now - timedelta(days=2),
                        completed_at=now - timedelta(days=2),
                        created_at=now - timedelta(days=2),
                        staging_cleanup_at=now - timedelta(minutes=1),
                    )
                    for _ in range(300)
                ])
            queries = []

            def count_sql(_connection, _cursor, statement, _parameters, _context, _many):
                queries.append(statement)

            event.listen(connection, "before_cursor_execute", count_sql)
            try:
                repository = PostgresArtifactStagingCleanup(sessions)
                claimed = repository.claim(now, limit=10, lease=timedelta(minutes=15))
                settled = repository.settle_many(tuple(
                    StagingSettlement(item, now + timedelta(days=1)) for item in claimed
                ))
            finally:
                event.remove(connection, "before_cursor_execute", count_sql)
            assert len(claimed) == len(settled) == 10
            assert sum("UPDATE terminal_artifacts" in sql for sql in queries) == 2
            business_queries = [
                sql for sql in queries
                if not sql.startswith(("SAVEPOINT", "RELEASE SAVEPOINT"))
            ]
            assert len(business_queries) == 3
            outer.rollback()
    finally:
        engine.dispose()
