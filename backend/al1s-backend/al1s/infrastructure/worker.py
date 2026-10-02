from __future__ import annotations

import signal
from collections.abc import Callable
from threading import Event
from typing import cast

import structlog

from al1s.adapters.mqtt.mosquitto import MosquittoDynamicSecurityBroker
from al1s.adapters.mqtt.publisher import MqttOutboxPublisher
from al1s.adapters.mqtt.recovering import RecoveringPublisher
from al1s.adapters.postgres.mqtt_unit_of_work import MqttSessionSqlAlchemyUnitOfWork
from al1s.adapters.postgres.notification_unit_of_work import NotificationSqlAlchemyUnitOfWork
from al1s.adapters.postgres.unit_of_work import SqlAlchemyUnitOfWork
from al1s.app.config import get_settings
from al1s.app.logging import configure_logging
from al1s.execution.mqtt_sessions import (
    MqttSessionPasswordSigner,
    TerminalMqttSessionService,
    TerminalMqttSessionUnitOfWork,
)
from al1s.infrastructure.database import create_database_engine, create_session_factory
from al1s.kernel.outbox_dispatcher import OutboxDispatcher
from al1s.kernel.ports import UnitOfWork
from al1s.notifications.event_publisher import NotificationEventPublisher
from al1s.notifications.security import FernetSecretCipher
from al1s.notifications.service import NotificationService, UowFactory


def main() -> None:
    configure_logging()
    settings = get_settings()
    stop = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    broker = MosquittoDynamicSecurityBroker(
        host=settings.mqtt_host,
        port=settings.mqtt_port,
        admin_username=settings.mqtt_username,
        admin_password=settings.mqtt_password.get_secret_value(),
        timeout_seconds=settings.probe_timeout_seconds,
        tls_enabled=settings.mqtt_tls_enabled,
    )
    mqtt_sessions = TerminalMqttSessionService(
        cast(
            Callable[[], TerminalMqttSessionUnitOfWork],
            lambda: MqttSessionSqlAlchemyUnitOfWork(sessions),
        ),
        broker,
        MqttSessionPasswordSigner(settings.mqtt_session_signing_key.get_secret_value()),
        public_host=settings.mqtt_public_host,
        public_port=settings.mqtt_public_port,
        tls_enabled=settings.mqtt_public_tls_enabled,
    )

    def connect() -> MqttOutboxPublisher:
        broker.ensure_platform_publisher(
            username=settings.mqtt_publisher_username,
            password=settings.mqtt_publisher_password.get_secret_value(),
            client_id="al1s-platform-publisher",
        )
        return MqttOutboxPublisher(
            host=settings.mqtt_host,
            port=settings.mqtt_port,
            username=settings.mqtt_publisher_username,
            password=settings.mqtt_publisher_password.get_secret_value(),
            timeout_seconds=settings.probe_timeout_seconds,
            tls_enabled=settings.mqtt_tls_enabled,
        )

    publisher = RecoveringPublisher(connect)
    dispatcher = OutboxDispatcher(
        uow_factory=cast(
            Callable[[], UnitOfWork],
            lambda: SqlAlchemyUnitOfWork(sessions),
        ),
        publisher=NotificationEventPublisher(
            publisher,
            NotificationService(
                cast(UowFactory, lambda: NotificationSqlAlchemyUnitOfWork(sessions)),
                FernetSecretCipher(settings.notification_master_key.get_secret_value()),
            ),
        ),
        worker_id="platform-mqtt-outbox",
    )
    logger = structlog.get_logger()
    try:
        while not stop.is_set():
            try:
                result = dispatcher.dispatch_once()
                revoked = mqtt_sessions.revoke_expired()
                if result.claimed or revoked:
                    logger.info(
                        "platform_worker_cycle",
                        claimed=result.claimed,
                        published=result.published,
                        failed=result.failed,
                        stale=result.stale,
                        mqtt_sessions_revoked=revoked,
                    )
            except Exception as exc:
                logger.error("platform_worker_retry", error_type=type(exc).__name__)
            stop.wait(settings.worker_interval_seconds)
    finally:
        publisher.close()
        engine.dispose()


if __name__ == "__main__":
    main()
