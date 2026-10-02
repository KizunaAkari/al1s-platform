import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from al1s.adapters.postgres import artifact_models, maa_models, scheduling_models  # noqa: F401
from al1s.adapters.postgres.base import Base
from al1s.adapters.postgres.blob_references import BLOB_REFERENCE_OWNERS, no_blob_references
from al1s.adapters.postgres.models import AuditLogRow, BlobObjectRow, OutboxEventRow
from al1s.adapters.postgres.technical_retention import PostgresTechnicalRetention


def test_all_business_blob_foreign_keys_have_registered_ownership():
    references = {
        table.name for table in Base.metadata.tables.values()
        if table.name != "gc_jobs"
        and any(fk.target_fullname == "blob_objects.id" for fk in table.foreign_keys)
    }
    assert references == set(BLOB_REFERENCE_OWNERS)
    sql = str(select(BlobObjectRow.id).where(*no_blob_references()))
    for reference in references:
        assert reference in sql
    assert sql.count("NOT (EXISTS") == len(references)


def test_cleanup_is_bounded_scoped_and_locked():
    session = MagicMock()
    session.scalars.return_value.all.return_value = []
    assert PostgresTechnicalRetention(session, owner_module="maa").redact(
        datetime.now(UTC), limit=5,
    ) == (0, 0)
    assert session.scalars.call_count == 2
    for call in session.scalars.call_args_list:
        sql = str(call.args[0].compile(dialect=postgresql.dialect()))
        assert "owner_module =" in sql
        assert "SKIP LOCKED" in sql
        assert "LIMIT" in sql
        assert "details_purged_at IS NULL" in sql
        assert "DELETE" not in sql


def test_unknown_owner_is_not_cleaned():
    with pytest.raises(ValueError):
        PostgresTechnicalRetention(MagicMock(), owner_module="unassigned")


@pytest.mark.parametrize("limit", [0, 51])
def test_invalid_batch_rejected(limit):
    with pytest.raises(ValueError):
        PostgresTechnicalRetention(MagicMock(), owner_module="maa").redact(
            datetime.now(UTC), limit=limit,
        )


def test_postgres_retention_preserves_state_and_other_owners():
    url = os.environ.get("AL1S_RETENTION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires explicitly selected isolated PostgreSQL")
    engine = create_engine(url)
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=7)
    try:
        with Session(engine) as session:
            # Registration alone is insufficient: every reference also needs
            # the database lock trigger that closes the GC/reference race.
            installed = set(session.scalars(text(
                "SELECT c.relname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_proc p ON p.oid = t.tgfoid "
                "WHERE p.proname = 'al1s_check_blob_reference' AND NOT t.tgisinternal"
            )))
            assert set(BLOB_REFERENCE_OWNERS) <= installed
            rows = []
            for owner, status, published in [
                ("maa", "published", cutoff),
                ("maa", "published", cutoff + timedelta(seconds=1)),
                ("maa", "pending", None),
                ("maa", "processing", None),
                ("maa", "dead_letter", None),
                ("information", "published", cutoff),
                ("unassigned", "published", cutoff),
            ]:
                row = OutboxEventRow(
                    id=uuid4(), owner_module=owner, event_type="retention.test",
                    schema_version=1, aggregate_type="test", aggregate_id=uuid4(),
                    correlation_id=uuid4(), occurred_at=cutoff, created_at=cutoff,
                    available_at=cutoff, payload={"debug": "old"}, status=status,
                    published_at=published, locked_by="test" if status == "processing" else None,
                    locked_until=now if status == "processing" else None,
                )
                session.add(row)
                rows.append(row)
            audit = AuditLogRow(
                owner_module="maa", actor_type="test", action="test", target_type="test",
                correlation_id=uuid4(), details={"debug": "old"}, summary="preserved",
                created_at=cutoff,
            )
            session.add(audit)
            session.flush()
            retention = PostgresTechnicalRetention(session, owner_module="maa")
            assert retention.redact(now) == (1, 1)
            assert retention.redact(now) == (0, 0)
            session.expire_all()
            assert rows[0].payload == {}
            assert rows[0].details_purged_at == now
            assert rows[0].status == "published"
            assert all(row.payload == {"debug": "old"} for row in rows[1:])
            assert audit.details == {} and audit.summary == "preserved"
            session.rollback()
    finally:
        engine.dispose()
