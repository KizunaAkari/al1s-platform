from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.bot_models import BotServiceRow
from al1s.adapters.postgres.quant_models import QuantReplyRow, QuantSendGateRow, QuantSettingsRow
from al1s.adapters.postgres.quant_replies import QuantReplies
from al1s.meme_workshop.contracts import CommandSource
from al1s.quant.commands import QuantCommand


@pytest.fixture
def queue():
    engine = create_engine("sqlite://")
    for table in (BotServiceRow.__table__, QuantSettingsRow.__table__, QuantSendGateRow.__table__, QuantReplyRow.__table__):
        table.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = uuid4()
    with sessions.begin() as session:
        session.add(BotServiceRow(id=service, kind="onebot_gateway", name="QQ", enabled=True,
                                  row_version=1))
    return QuantReplies(sessions), service


def command(message_id, sender):
    return QuantCommand(CommandSource(message_id=message_id, scope_kind="qq_group", target_id="123",
                                      sender_id=sender), "help", (), 1, 1790000000)


def test_global_gate_spaces_different_users_and_unknown_is_not_retried(queue):
    replies, service = queue
    first, created = replies.enqueue(service, command("1", "1"), "帮助", 10)
    replay, created_again = replies.enqueue(service, command("1", "1"), "其他结果", 10)
    assert created and not created_again and replay.id == first.id
    second, _ = replies.enqueue(service, command("2", "2"), "帮助", 10)
    now = datetime(2026, 10, 27, 2, 0, tzinfo=UTC)
    assert replies.claim_next(now, eligible_services=set()) is None
    assert replies.claim_next(now).id == first.id
    replies.settle(first.id, "unknown", now)
    assert replies.claim_next(now + timedelta(seconds=9)) is None
    assert replies.claim_next(now + timedelta(seconds=10)).id == second.id
    assert replies.recover_sending(now + timedelta(seconds=11)) == 1
    assert replies.claim_next(now + timedelta(seconds=20)) is None


def test_global_gate_uses_the_strictest_enabled_connection_interval(queue):
    replies, service = queue
    with replies.sessions.begin() as session:
        session.add(QuantSettingsRow(service_id=service, enabled=True, reply_interval_seconds=10))
    replies.enqueue(service, command("1", "1"), "一", 5)
    replies.enqueue(service, command("2", "2"), "二", 5)
    now = datetime(2026, 10, 27, 2, 0, tzinfo=UTC)
    assert replies.claim_next(now) is not None
    assert replies.claim_next(now + timedelta(seconds=5)) is None
    assert replies.claim_next(now + timedelta(seconds=10)) is not None


def test_increased_global_interval_takes_effect_before_next_reply(queue):
    replies, service = queue
    with replies.sessions.begin() as session:
        session.add(QuantSettingsRow(service_id=service, enabled=True, reply_interval_seconds=5))
    replies.enqueue(service, command("1", "1"), "一", 5)
    replies.enqueue(service, command("2", "2"), "二", 5)
    now = datetime(2026, 10, 27, 2, 0, tzinfo=UTC)
    assert replies.claim_next(now) is not None
    with replies.sessions.begin() as session:
        session.get(QuantSettingsRow, service).reply_interval_seconds = 10
    assert replies.claim_next(now + timedelta(seconds=5)) is None
    assert replies.claim_next(now + timedelta(seconds=10)) is not None


def test_png_outbox_deduplicates_and_releases_bytes_after_unknown_send(queue):
    replies, service = queue
    first, created = replies.enqueue(service, command("chart-1", "1"), "BTC行情", 2,
                                     image=b"\x89PNG\r\n\x1a\nexample")
    replay, again = replies.enqueue(service, command("chart-1", "1"), "BTC行情", 2,
                                    image=b"\x89PNG\r\n\x1a\nchanged")
    assert created and not again and replay.id == first.id
    now = datetime.now(UTC)
    sent = replies.claim_next(now)
    assert sent.image == first.image and sent.expires_at is not None
    replies.settle(sent.id, "unknown", now)
    with replies.sessions() as session:
        assert session.get(QuantReplyRow, sent.id).image is None
    assert replies.claim_next(now + timedelta(seconds=3)) is None


def test_expired_chart_is_not_claimed_and_drops_png(queue):
    replies, service = queue
    item, _ = replies.enqueue(service, command("chart-2", "1"), "BTC行情", 2,
                              image=b"\x89PNG\r\n\x1a\nexample")
    assert replies.claim_next(datetime.now(UTC) + timedelta(minutes=16)) is None
    with replies.sessions() as session:
        row = session.get(QuantReplyRow, item.id)
        assert row.status == "unknown" and row.image is None
