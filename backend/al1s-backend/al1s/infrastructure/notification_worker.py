"""Dedicated notification process: slow providers cannot stall task scheduling."""

from __future__ import annotations

import signal
import time
from collections.abc import Callable
from threading import Event
from typing import Protocol, cast
from uuid import uuid4

import structlog

from al1s.adapters.notifications.discord import DiscordNotificationAdapter
from al1s.adapters.notifications.onebot import OneBotNotificationAdapter
from al1s.adapters.notifications.smtp import SmtpNotificationAdapter
from al1s.adapters.postgres.bot_unit_of_work import BotSqlAlchemyUnitOfWork
from al1s.adapters.postgres.lexicon_repository import PostgresLexiconRepository
from al1s.adapters.postgres.notification_evidence import PostgresNotificationEvidence
from al1s.adapters.postgres.notification_unit_of_work import NotificationSqlAlchemyUnitOfWork
from al1s.adapters.s3.blob_store import S3BlobStore
from al1s.app.config import get_settings
from al1s.app.logging import configure_logging
from al1s.bots.ports import BotUnitOfWork
from al1s.bots.runtime_connection import BotConnectionReader
from al1s.execution.notification_evidence import ExecutionNotificationEvidence
from al1s.infrastructure.database import create_database_engine, create_session_factory
from al1s.notifications.dispatcher import NotificationDispatcher
from al1s.notifications.lexicon_service import LexiconService
from al1s.notifications.ports import NotificationUnitOfWork
from al1s.notifications.review_service import ReviewService
from al1s.notifications.security import FernetSecretCipher
from al1s.notifications.types import ChannelKind, NotificationDispatchResult


class NotificationDispatcherPort(Protocol):
    def dispatch_once(self) -> NotificationDispatchResult: ...


class WorkerLogger(Protocol):
    def error(self, event: str, **event_kw: object) -> object: ...

    def info(self, event: str, **event_kw: object) -> object: ...


def _dispatch_notifications(
    dispatcher: NotificationDispatcherPort,
    logger: WorkerLogger,
) -> NotificationDispatchResult:
    try:
        return dispatcher.dispatch_once()
    except Exception as exc:
        # Tracebacks/SQL parameters can contain payloads or credentials.
        logger.error("notification_dispatch_cycle_failed", error=type(exc).__name__)
        return NotificationDispatchResult(0, 0, 0, 0, 0)


def run_loop(
    dispatcher: NotificationDispatcherPort,
    logger: WorkerLogger,
    stop: Event,
    *,
    interval: float,
    maintenance: Callable[[], int] | None = None,
) -> None:
    if interval <= 0:
        raise ValueError("notification worker interval must be positive")
    maintenance_at = 0.0
    while not stop.is_set():
        if maintenance is not None and time.monotonic() >= maintenance_at:
            try:
                maintenance()
            except Exception as exc:
                logger.error("review_maintenance_failed", error=type(exc).__name__)
            maintenance_at = time.monotonic() + 10
        result = _dispatch_notifications(dispatcher, logger)
        if result.claimed:
            logger.info(
                "notification_worker_cycle",
                claimed=result.claimed,
                sent=result.sent,
                failed=result.failed,
                dead_lettered=result.dead_lettered,
                stale=result.stale,
            )
        stop.wait(interval)


def main() -> None:
    configure_logging()
    settings = get_settings().model_copy(
        update={"database_pool_size": 2, "database_max_overflow": 0}
    )
    stop = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    cipher = FernetSecretCipher(settings.notification_master_key.get_secret_value())
    connections = BotConnectionReader(
        cast(Callable[[], BotUnitOfWork], lambda: BotSqlAlchemyUnitOfWork(sessions)), cipher,
    )
    review = ReviewService(
        cast(
            Callable[[], NotificationUnitOfWork],
            lambda: NotificationSqlAlchemyUnitOfWork(sessions),
        ),
        cipher, LexiconService(PostgresLexiconRepository(sessions)),
    )
    evidence_store = S3BlobStore(settings)
    evidence = ExecutionNotificationEvidence(PostgresNotificationEvidence(sessions), evidence_store)
    try:
        dispatcher = NotificationDispatcher(
            uow_factory=cast(
                Callable[[], NotificationUnitOfWork],
                lambda: NotificationSqlAlchemyUnitOfWork(sessions),
            ),
            adapters={
                ChannelKind.SMTP: SmtpNotificationAdapter(),
                ChannelKind.QQ: OneBotNotificationAdapter(connections),
                ChannelKind.DISCORD: DiscordNotificationAdapter(connections),
            },
            cipher=cipher,
            worker_id=f"notifications-{uuid4()}",
            review_body=review.sending_body,
            platform_public_url=settings.platform_public_url,
            execution_evidence=evidence.read,
        )
        run_loop(
            dispatcher, structlog.get_logger(), stop, interval=settings.worker_interval_seconds,
            maintenance=review.maintain,
        )
    finally:
        evidence_store.close()
        engine.dispose()


if __name__ == "__main__":
    main()
