from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock

from al1s.kernel.outbox_dispatcher import OutboxDispatcher


def test_batch_stops_sending_when_claim_deadline_passes():
    now = [datetime(2026, 9, 21, tzinfo=UTC)]
    uow = MagicMock()
    uow.__enter__.return_value = uow
    events = [Mock(), Mock(), Mock()]
    uow.outbox.claim_batch.return_value = events
    uow.outbox.mark_published.return_value = 0
    uow.outbox.mark_failed.return_value = 0
    publisher = Mock()

    def send(event):
        now[0] += timedelta(seconds=31)

    publisher.publish.side_effect = send
    result = OutboxDispatcher(
        uow_factory=lambda: uow, publisher=publisher, worker_id="test", now=lambda: now[0]
    ).dispatch_once()
    publisher.publish.assert_called_once_with(events[0])
    assert result.stale == 3
