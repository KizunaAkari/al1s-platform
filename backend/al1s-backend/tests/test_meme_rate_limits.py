from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.adapters.postgres.meme_rate_limit import finish_send, reserve_send


@pytest.mark.parametrize(("platform", "interval"), [("discord", 5), ("qq", 10), ("qq", 47)])
def test_reservation_uses_saved_platform_interval_and_keeps_unknown_send_hold(platform, interval):
    now = datetime.now(UTC)
    gate = SimpleNamespace(platform=platform, interval_seconds=interval, available_at=now)
    session = MagicMock()
    session.get.return_value = gate
    identity = uuid4()
    assert reserve_send(session, platform, identity, now)
    assert gate.owner_receipt_id == identity
    assert gate.available_at == now + timedelta(seconds=interval + 20)


def test_settlement_waits_the_current_saved_interval(monkeypatch):
    now = datetime(2026, 10, 9, tzinfo=UTC)
    clock = MagicMock()
    clock.now.return_value = now
    monkeypatch.setattr("al1s.adapters.postgres.meme_rate_limit.datetime", clock)
    identity = uuid4()
    gate = SimpleNamespace(platform="discord", interval_seconds=17, owner_receipt_id=identity)
    session = MagicMock()
    session.scalar.return_value = gate
    finish_send(session, identity)
    assert gate.owner_receipt_id is None
    assert gate.available_at == now + timedelta(seconds=17)
