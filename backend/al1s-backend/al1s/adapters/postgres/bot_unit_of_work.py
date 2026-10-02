from __future__ import annotations

from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.bot_repositories import (
    PostgresBotConfigRepository,
    PostgresBotHealthRepository,
    PostgresBotRegistrationGrantRepository,
    PostgresBotServiceRepository,
    PostgresBotWorkerIdentityRepository,
)
from al1s.adapters.postgres.repositories import PostgresAuditRepository, PostgresOutboxRepository
from al1s.adapters.postgres.secret_repository import PostgresSecretRepository


class BotSqlAlchemyUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    def __enter__(self) -> BotSqlAlchemyUnitOfWork:
        if self._session is not None:
            raise RuntimeError("unit of work cannot be entered twice")
        session = self._session_factory()
        self._session = session
        self.services = PostgresBotServiceRepository(session)
        self.grants = PostgresBotRegistrationGrantRepository(session)
        self.identities = PostgresBotWorkerIdentityRepository(session)
        self.configs = PostgresBotConfigRepository(session)
        self.health = PostgresBotHealthRepository(session)
        self.secrets = PostgresSecretRepository(session)
        self.audit = PostgresAuditRepository(session, owner_module="information")
        self.outbox = PostgresOutboxRepository(session, owner_module="information")
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

    def flush(self) -> None:
        self._require_session().flush()

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        return self._session
