from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.quant_instruments import QuantInstruments, infer_market
from al1s.adapters.postgres.quant_models import QuantInstrumentRow


def test_bse_old_and_new_codes_resolve_to_one_stable_instrument():
    engine = create_engine("sqlite://")
    QuantInstrumentRow.__table__.create(engine)
    instruments = QuantInstruments(sessionmaker(engine, expire_on_commit=False))
    first = instruments.register("BJ", "430047", date(2026, 10, 27))
    second = instruments.register("BJ", "920047", date(2026, 10, 27))
    assert first.id == second.id and first.current_code == "920047" and first.board == "beijing"
    assert infer_market("688001") == "SH" and infer_market("300750") == "SZ"
    assert infer_market("430047") == "BJ"
