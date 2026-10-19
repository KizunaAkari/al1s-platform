from __future__ import annotations

from al1s.adapters.postgres.bot_repositories import (
    PostgresBotConfigRepository,
    PostgresBotHealthRepository,
    PostgresBotRegistrationGrantRepository,
    PostgresBotServiceRepository,
    PostgresBotWorkerIdentityRepository,
)
from al1s.adapters.postgres.repositories import PostgresAuditRepository, PostgresOutboxRepository
from al1s.adapters.postgres.secret_repository import PostgresSecretRepository
from al1s.adapters.postgres.transaction_scope import ExplicitCommitUnitOfWork


class BotSqlAlchemyUnitOfWork(ExplicitCommitUnitOfWork):

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

    def flush(self) -> None:
        self._require_session().flush()
