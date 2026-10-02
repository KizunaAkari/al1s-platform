from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from al1s.notifications.lease import DeliveryLease


def _lease():
    clock = [datetime(2026, 9, 9, tzinfo=UTC)]
    item = MagicMock(started_at=clock[0])
    factory = MagicMock()
    uow = factory.return_value.__enter__.return_value
    uow.deliveries.renew_batch.return_value = 1
    lease = DeliveryLease(
        factory,
        [item],
        worker_id="test",
        duration=timedelta(seconds=30),
        now=lambda: clock[0],
    )
    return lease, clock, uow


def test_renew_extends_deadline_without_resetting_after_expiry():
    lease, clock, uow = _lease()
    clock[0] += timedelta(seconds=20)
    assert lease.renew()
    uow.commit.assert_called_once()
    clock[0] += timedelta(seconds=20)
    assert lease.valid
    clock[0] += timedelta(seconds=10)
    assert not lease.valid
    assert not lease.renew()
    assert uow.deliveries.renew_batch.call_count == 1


def test_partial_renewal_stops_sending_and_does_not_commit():
    lease, _clock, uow = _lease()
    uow.deliveries.renew_batch.return_value = 0
    assert not lease.renew()
    assert not lease.valid
    uow.commit.assert_not_called()


def test_database_failure_invalidates_lease():
    lease, _clock, uow = _lease()
    uow.deliveries.renew_batch.side_effect = RuntimeError("private database details")
    assert not lease.renew()
    assert not lease.valid
    uow.commit.assert_not_called()
