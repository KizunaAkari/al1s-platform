from __future__ import annotations

from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.repositories import (
    PostgresAuditRepository,
    PostgresBlobCatalogRepository,
    PostgresGcJobRepository,
    PostgresInboxRepository,
    PostgresOutboxRepository,
)


class SqlAlchemyUnitOfWork:
    """Platform transaction: commit explicitly; exit rolls back uncommitted changes.

    A caught exception does not abort the context automatically. Call rollback
    before continuing if a failed operation must discard pending writes.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

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

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._require_session()
        try:
            if exc_type is not None or session.in_transaction():
                session.rollback()
        finally:
            session.close()
            self._session = None

    def commit(self) -> None:
        self._require_session().commit()

    def rollback(self) -> None:
        self._require_session().rollback()

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        return self._session
