from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.notifications.errors import NotificationDomainError
from al1s.notifications.service import NotificationService
from al1s.notifications.types import NotificationKind


def test_forward_cannot_bypass_review_through_internal_service():
    factory = MagicMock()
    service = NotificationService(factory, MagicMock())
    with pytest.raises(NotificationDomainError, match="review ingress"):
        service.materialize_intent(
            source_event_id=uuid4(), notification_kind=NotificationKind.FORWARD,
            source_type="discord", source_id=uuid4(), correlation_id=uuid4(),
            payload={"summary": "unchecked"}, occurred_at=datetime.now(UTC),
        )
    factory.assert_not_called()


@pytest.mark.parametrize("concurrent", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_replay_content_is_checked_in_both_paths(concurrent, changed):
    now = datetime.now(UTC)
    source_id, event_id = uuid4(), uuid4()
    existing = SimpleNamespace(
        notification_kind=NotificationKind.STORAGE_LOW, source_type="storage",
        source_id=source_id, payload={"summary": "first"}, occurred_at=now,
    )
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.intents.get_by_source_event.side_effect = [None, existing] if concurrent else [existing]
    uow.intents.add.return_value = False
    uow.routes.list_enabled_for_kind.return_value = []
    service = NotificationService(lambda: uow, MagicMock(), now=lambda: now)
    args = dict(
        source_event_id=event_id, notification_kind=NotificationKind.STORAGE_LOW,
        source_type="storage", source_id=source_id, correlation_id=uuid4(),
        payload={"summary": "changed" if changed else "first"}, occurred_at=now,
    )
    if changed:
        with pytest.raises(NotificationDomainError) as error:
            service.materialize_intent(**args)
        assert error.value.status_code == 409
    else:
        assert service.materialize_intent(**args) is existing
    uow.deliveries.add_many.assert_not_called()
    uow.commit.assert_not_called()
