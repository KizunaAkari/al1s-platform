from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from al1s.adapters.postgres.execution_repositories import PostgresTerminalRepository
from al1s.execution.terminal_health import TerminalHealthMonitor
from al1s.kernel.types import ClaimedOutboxEvent
from al1s.notifications.event_publisher import NotificationEventPublisher
from al1s.notifications.types import NotificationKind


def test_offline_transition_and_event_share_transaction():
    now = datetime.now(UTC)
    uow = MagicMock()
    uow.__enter__.return_value = uow
    terminal_id = uuid4()
    uow.terminals.expire_heartbeats.return_value = [
        SimpleNamespace(terminal_id=terminal_id, row_version=3)
    ]
    monitor = TerminalHealthMonitor(lambda: uow, now=lambda: now)
    assert monitor.check_once() == 1
    uow.terminals.expire_heartbeats.assert_called_once_with(now - timedelta(seconds=90), limit=100)
    event = uow.outbox.add.call_args.args[0]
    assert event.aggregate_id == terminal_id and event.event_type == "terminal.offline.v1"
    uow.commit.assert_called_once()


def test_event_failure_does_not_commit_offline_transition():
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.terminals.expire_heartbeats.return_value = [
        SimpleNamespace(terminal_id=uuid4(), row_version=3)
    ]
    uow.outbox.add.side_effect = RuntimeError("write failed")
    with pytest.raises(RuntimeError):
        TerminalHealthMonitor(lambda: uow).check_once()
    uow.commit.assert_not_called()


def test_expiry_query_is_bounded_locked_and_preserves_last_seen():
    session = MagicMock()
    session.scalars.return_value.all.return_value = []
    cutoff = datetime.now(UTC)
    assert PostgresTerminalRepository(session).expire_heartbeats(cutoff, limit=100) == []
    query = session.scalars.call_args.args[0].compile(dialect=postgresql.dialect())
    sql = str(query)
    assert "FOR UPDATE SKIP LOCKED" in sql and "LIMIT" in sql
    assert "last_seen_at=" not in sql
    assert cutoff in query.params.values() and 100 in query.params.values()
    session.scalars.assert_called_once()


def test_terminal_alert_uses_dedicated_kind_and_stable_event_identity():
    downstream, notifications = Mock(), Mock()
    event = ClaimedOutboxEvent(
        event_id=uuid4(), event_type="terminal.offline.v1", schema_version=1,
        aggregate_type="terminal", aggregate_id=uuid4(), correlation_id=uuid4(),
        occurred_at=datetime.now(UTC), payload={"reason": "heartbeat_timeout"},
        attempt_count=1, row_version=1,
    )
    publisher = NotificationEventPublisher(downstream, notifications)
    publisher.publish(event)
    publisher.publish(event)
    first, second = notifications.materialize_intent.call_args_list
    assert first == second
    assert first.kwargs["notification_kind"] == NotificationKind.TERMINAL_ALERT
    assert first.kwargs["source_event_id"] == event.event_id
