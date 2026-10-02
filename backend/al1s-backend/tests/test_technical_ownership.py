from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from al1s.adapters.postgres.repositories import PostgresAuditRepository, PostgresOutboxRepository
from al1s.kernel.types import NewAuditEntry, NewOutboxEvent


def test_outbox_owner_is_assigned_by_repository() -> None:
    session = MagicMock()
    event = NewOutboxEvent(
        uuid4(), "test", 1, "notification_channel", uuid4(), uuid4(),
        datetime.now(UTC), {"owner_module": "maa"},
    )
    PostgresOutboxRepository(session, owner_module="information").add(event)
    assert session.add.call_args.args[0].owner_module == "information"


def test_audit_owner_is_assigned_by_repository() -> None:
    session = MagicMock()
    entry = NewAuditEntry(uuid4(), "user", None, "test", "test", None, uuid4(), {})
    PostgresAuditRepository(session, owner_module="maa").add(entry)
    assert session.add.call_args.args[0].owner_module == "maa"


def test_claim_is_scoped() -> None:
    session = MagicMock()
    session.execute.return_value.all.return_value = []
    PostgresOutboxRepository(session, owner_module="information").claim_batch(
        worker_id="test", now=datetime.now(UTC), lease_duration=timedelta(seconds=30), limit=5,
    )
    sql = str(session.execute.call_args.args[0].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    assert "outbox_events.owner_module = 'information'" in sql
    assert "SKIP LOCKED" in sql
    assert "LIMIT 5" in sql


@pytest.mark.parametrize("factory", [PostgresAuditRepository, PostgresOutboxRepository])
def test_invalid_owner_is_rejected(factory) -> None:
    with pytest.raises(ValueError, match="unknown module owner"):
        factory(MagicMock(), owner_module="arbitrary")
