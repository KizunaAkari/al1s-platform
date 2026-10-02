from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from al1s.adapters.postgres.repositories import PostgresAuditRepository
from al1s.kernel.types import NewAuditEntry


def test_audit_repository_rejects_nested_sensitive_fields() -> None:
    repository = PostgresAuditRepository(Session())
    entry = NewAuditEntry(
        audit_id=uuid4(),
        actor_type="system",
        actor_id=None,
        action="settings.updated",
        target_type="settings",
        target_id=None,
        correlation_id=uuid4(),
        details={"safe": {"api-token": "must-not-be-stored"}},
    )

    with pytest.raises(ValueError, match="sensitive audit field"):
        repository.add(entry)
