from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.quant_market_data import QuantMarketData
from al1s.adapters.postgres.quant_models import QuantBarRow, QuantInstrumentRow, QuantQuoteRow
from al1s.quant.market_data import MarketBar


def test_cache_uses_provider_time_identity_and_never_overwrites_with_older_quote():
    engine = create_engine("sqlite://")
    for table in (QuantInstrumentRow.__table__, QuantBarRow.__table__, QuantQuoteRow.__table__):
        table.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    instrument = uuid4()
    with sessions.begin() as session:
        session.add(QuantInstrumentRow(id=instrument, market="SH", current_code="600000",
                                       board="main", name="stock", active=True))
    cache = QuantMarketData(sessions)
    now = datetime(2026, 10, 27, 7, tzinfo=UTC)
    one = MarketBar(now, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), 1000)
    newer = MarketBar(now + timedelta(minutes=1), Decimal("11"), Decimal("12"), Decimal("10"),
                      Decimal("11"), 2000)
    cache.store(instrument, "sina", "1m", "", [one, newer], fetched_at=now + timedelta(minutes=2))
    cache.store(instrument, "sina", "1m", "", [one], fetched_at=now + timedelta(minutes=3))
    with sessions() as session:
        assert session.query(QuantBarRow).count() == 2
        assert session.get(QuantQuoteRow, (instrument, "sina")).price == Decimal("11")
    assert len(cache.read(instrument, "sina", "1m", "", start=now, end=now + timedelta(minutes=1))) == 2
    for provider in ("tencent", "tushare", "gugudata"):
        cache.store(instrument, provider, "1m", "", [one], fetched_at=now + timedelta(minutes=2))
        assert len(cache.read(instrument, provider, "1m", "", start=now, end=now)) == 1
        with sessions() as session:
            assert session.get(QuantQuoteRow, (instrument, provider)).price == Decimal("10")
