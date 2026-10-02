from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from al1s.adapters.postgres.notification_repositories import PostgresNotificationDeliveryRepository
from al1s.notifications.errors import NotificationDomainError
from al1s.notifications.query_service import NotificationQueryService


@pytest.mark.parametrize("disposition,counts,expected", [
    ("no_route", {}, "no_route"),
    ("queued", {}, "inconsistent"),
    ("queued", {"sent": 2}, "sent"),
    ("queued", {"sent": 1, "pending": 1}, "processing"),
    ("queued", {"processing": 1}, "processing"),
    ("queued", {"sent": 1, "dead_letter": 1}, "partial_failure"),
    ("queued", {"sent": 1, "cancelled": 1}, "partial_failure"),
    ("queued", {"cancelled": 2}, "cancelled"),
    ("queued", {"dead_letter": 1}, "failed"),
])
def test_source_summary(disposition, counts, expected):
    uow = MagicMock()
    uow.__enter__.return_value = uow
    intent_id, event_id = uuid4(), uuid4()
    uow.intents.get_by_source_event.return_value = SimpleNamespace(
        intent_id=intent_id, disposition=disposition,
    )
    uow.deliveries.count_statuses.return_value = counts
    result = NotificationQueryService(lambda: uow).source_summary(event_id)
    assert result.status == expected
    assert result.intent_id == intent_id and result.source_event_id == event_id
    assert result.counts == counts
    uow.deliveries.count_statuses.assert_called_once_with(intent_id)


def test_unknown_source_is_not_a_success():
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.intents.get_by_source_event.return_value = None
    with pytest.raises(NotificationDomainError) as error:
        NotificationQueryService(lambda: uow).source_summary(uuid4())
    assert error.value.status_code == 404
    uow.deliveries.count_statuses.assert_not_called()


def test_counts_are_aggregated_in_database_without_payload():
    session = MagicMock()
    session.execute.return_value.all.return_value = [("sent", 3)]
    result = PostgresNotificationDeliveryRepository(session).count_statuses(uuid4())
    assert result == {"sent": 3}
    assert session.execute.call_count == 1
    sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "GROUP BY" in sql and "intent_id =" in sql
    assert "payload" not in sql
