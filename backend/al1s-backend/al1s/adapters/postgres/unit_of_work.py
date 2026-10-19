from __future__ import annotations

from al1s.adapters.postgres.repositories import (
    PostgresAuditRepository,
    PostgresBlobCatalogRepository,
    PostgresGcJobRepository,
    PostgresInboxRepository,
    PostgresOutboxRepository,
)
from al1s.adapters.postgres.transaction_scope import ExplicitCommitUnitOfWork


class SqlAlchemyUnitOfWork(ExplicitCommitUnitOfWork):
    """Platform transaction: commit explicitly; exit rolls back uncommitted changes.

    A caught exception does not abort the context automatically. Call rollback
    before continuing if a failed operation must discard pending writes.
    """

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        if self._session is not None:
            raise RuntimeError("unit of work cannot be entered twice")
        session = self._session_factory()
        self._session = session
        self.outbox = PostgresOutboxRepository(session)
        self.inbox = PostgresInboxRepository(session)
        self.blobs = PostgresBlobCatalogRepository(session)
        self.gc_jobs = PostgresGcJobRepository(session)
        self.audit = PostgresAuditRepository(session)
        return self
