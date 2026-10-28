from uuid import uuid4
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.quant_corporate_sync import QuantCorporateSync
from al1s.adapters.postgres.quant_models import QuantAccountRow, QuantCorporateEventRow, QuantEntitlementRow, QuantInstrumentRow


def test_source_event_deduplicates_and_conflicting_terms_require_review():
    engine = create_engine("sqlite://")
    for table in (QuantAccountRow.__table__, QuantInstrumentRow.__table__, QuantCorporateEventRow.__table__, QuantEntitlementRow.__table__):
        table.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    stock = uuid4()
    with sessions.begin() as session:
        session.add(QuantInstrumentRow(id=stock, market="SZ", current_code="300750", board="main", name="x", active=True))
    row = {"报告时间": "2022年报", "分红类型": "年度分红", "股权登记日": "2023-04-25",
           "派息日": "2023-04-26", "股份到账日": "2023-04-26", "派息比例": "25.2",
           "送股比例": "nan", "转增比例": "8.0", "实施方案分红说明": "10转增8股派25.2元(含税)"}
    repo = QuantCorporateSync(sessions)
    repo.save(stock, [row])
    repo.save(stock, [row])
    with sessions() as session:
        event = session.query(QuantCorporateEventRow).one()
        assert event.state == "verified" and event.shares_per_share == Decimal("0.8")
    row["派息比例"] = "26"
    row["实施方案分红说明"] = "10转增8股派26元(含税)"
    repo.save(stock, [row])
    with sessions() as session:
        assert session.query(QuantCorporateEventRow).one().state == "needs_review"
