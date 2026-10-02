from __future__ import annotations

from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.bot_repositories import PostgresBotServiceRepository
from al1s.adapters.postgres.discord_forward_repository import PostgresDiscordForwardRepository
from al1s.adapters.postgres.notification_repositories import (
    PostgresNotificationChannelRepository,
    PostgresNotificationDeliveryRepository,
    PostgresNotificationIntentRepository,
    PostgresNotificationRouteRepository,
)
from al1s.adapters.postgres.repositories import PostgresAuditRepository, PostgresOutboxRepository
from al1s.adapters.postgres.review_repository import PostgresReviewRepository
from al1s.adapters.postgres.secret_repository import PostgresSecretRepository


class NotificationSqlAlchemyUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    def __enter__(self) -> NotificationSqlAlchemyUnitOfWork:
        if self._session is not None:
            raise RuntimeError("unit of work cannot be entered twice")
        session = self._session_factory()
        self._session = session
        self.bot_services = PostgresBotServiceRepository(session)
        self.forward_rules = PostgresDiscordForwardRepository(session)
        self.reviews = PostgresReviewRepository(session)
        self.secrets = PostgresSecretRepository(session)
        self.channels = PostgresNotificationChannelRepository(session)
        self.routes = PostgresNotificationRouteRepository(session)
        self.intents = PostgresNotificationIntentRepository(session)
        self.deliveries = PostgresNotificationDeliveryRepository(session)
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

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        return self._session
