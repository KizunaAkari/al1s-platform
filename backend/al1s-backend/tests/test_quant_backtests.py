from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.bot_models import BotServiceRow
from al1s.adapters.postgres.quant_accounts import QuantAccounts
from al1s.adapters.postgres.quant_backtests import QuantBacktests
from al1s.adapters.postgres.quant_models import QuantAccountRow, QuantBacktestRow, QuantInstrumentRow
from al1s.meme_workshop.contracts import CommandSource
from al1s.quant.backtest_engine import BacktestResult
from al1s.quant.commands import QuantCommand
from al1s.quant.contracts import QuantSettings
from tests.test_quant_settings import sqlite_jsonb as sqlite_jsonb


def test_backtest_event_is_idempotent_and_completed_result_survives_replay():
    engine = create_engine("sqlite://")
    for table in (BotServiceRow.__table__, QuantAccountRow.__table__, QuantInstrumentRow.__table__,
                  QuantBacktestRow.__table__):
        table.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    accounts, repo = QuantAccounts(sessions), QuantBacktests(sessions)
    accounts.open("123", Decimal("100000"))
    service, instrument = uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(BotServiceRow(id=service, name="qq", kind="onebot_gateway", enabled=True, row_version=1))
        session.add(QuantInstrumentRow(id=instrument, market="SH", current_code="600000", board="main", name="x", active=True))
    source = CommandSource(message_id="1", scope_kind="qq_group", target_id="555", sender_id="123")
    command = QuantCommand(source, "backtest", ("买入持有", "600000", "2026-01-01", "2026-02-01"),
                           1, 1790000000, settings_version=1, settings=QuantSettings())
    args = (service, command, instrument, "buy_hold", date(2026, 1, 1), date(2026, 2, 1), "sina", "")
    first, created = repo.begin(*args)
    assert created and first.status == "processing"
    replay, created = repo.begin(*args)
    assert not created and replay.id == first.id
    repo.complete(first.id, BacktestResult(Decimal("0.1"), Decimal("0.05"), 2, Decimal("110000"), "a"*64))
    replay, created = repo.begin(*args)
    assert not created and replay.status == "completed" and replay.total_return == Decimal("0.1")
    switched = (*args[:6], "tencent", args[7])
    replay, created = repo.begin(*switched)
    assert not created and replay.id == first.id and replay.provider == "sina"
    assert repo.find(service, command).provider == "sina"
