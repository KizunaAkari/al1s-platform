import os
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.execution_models import TerminalRow
from al1s.adapters.postgres.execution_unit_of_work import ExecutionSqlAlchemyUnitOfWork
from al1s.adapters.postgres.models import OutboxEventRow
from al1s.adapters.postgres.notification_models import NotificationIntentRow
from al1s.adapters.postgres.notification_unit_of_work import NotificationSqlAlchemyUnitOfWork
from al1s.execution.terminal_health import TerminalHealthMonitor
from al1s.kernel.types import ClaimedOutboxEvent
from al1s.notifications.event_publisher import NotificationEventPublisher
from al1s.notifications.security import FernetSecretCipher
from al1s.notifications.service import NotificationService


def test_expiry_recovery_and_notification_are_durable_and_idempotent():
    url = os.environ.get("AL1S_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated PostgreSQL required")
    engine = create_engine(url)
    now = datetime.now(UTC)
    identities = [uuid4() for _ in range(3)]
    with engine.connect() as connection:
        transaction = connection.begin()
        sessions = sessionmaker(bind=connection, expire_on_commit=False,
                                join_transaction_mode="create_savepoint")
        try:
            with sessions.begin() as session:
                for index, identity in enumerate(identities):
                    session.add(TerminalRow(
                        id=identity, installation_id=uuid4(), terminal_type="linux",
                        display_name="health-test", service_status="online",
                        acceptance_status="accepting", agent_version="test", row_version=1,
                        last_seen_at=now - timedelta(seconds=90 if index != 1 else 89),
                        deleted_at=now if index == 2 else None,
                    ))
            monitor = TerminalHealthMonitor(lambda: ExecutionSqlAlchemyUnitOfWork(sessions),
                                            now=lambda: now)
            assert monitor.check_once() == 1
            assert monitor.check_once() == 0
            with sessions() as session:
                terminal = session.get(TerminalRow, identities[0])
                assert terminal.service_status == "offline" and terminal.row_version == 2
                assert terminal.last_seen_at == now - timedelta(seconds=90)
                row = session.scalar(select(OutboxEventRow).where(
                    OutboxEventRow.aggregate_id == identities[0],
                    OutboxEventRow.event_type == "terminal.offline.v1",
                ))
                event = ClaimedOutboxEvent(
                    event_id=row.id, event_type=row.event_type, schema_version=row.schema_version,
                    aggregate_type=row.aggregate_type, aggregate_id=row.aggregate_id,
                    correlation_id=row.correlation_id, occurred_at=row.occurred_at,
                    payload=row.payload, attempt_count=1, row_version=row.row_version,
                )
            notifications = NotificationService(
                lambda: NotificationSqlAlchemyUnitOfWork(sessions),
                FernetSecretCipher("health-test-secret-" + "x" * 32),
            )
            publisher = NotificationEventPublisher(Mock(), notifications)
            publisher.publish(event)
            publisher.publish(event)
            with sessions.begin() as session:
                assert session.scalar(select(func.count()).select_from(NotificationIntentRow)
                    .where(NotificationIntentRow.source_event_id == event.event_id)) == 1
                intent = session.scalar(select(NotificationIntentRow).where(
                    NotificationIntentRow.source_event_id == event.event_id))
                assert intent.notification_kind == "terminal_alert"
                assert intent.disposition == "no_route"
                session.execute(update(TerminalRow).where(TerminalRow.id == identities[0])
                                .values(service_status="online"))
            assert monitor.check_once() == 1
            with sessions() as session:
                assert session.scalar(select(func.count()).select_from(OutboxEventRow).where(
                    OutboxEventRow.aggregate_id == identities[0],
                    OutboxEventRow.event_type == "terminal.offline.v1")) == 2
        finally:
            transaction.rollback()
    engine.dispose()
