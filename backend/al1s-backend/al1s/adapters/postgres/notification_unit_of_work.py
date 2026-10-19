from __future__ import annotations

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
from al1s.adapters.postgres.transaction_scope import ExplicitCommitUnitOfWork


class NotificationSqlAlchemyUnitOfWork(ExplicitCommitUnitOfWork):

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
