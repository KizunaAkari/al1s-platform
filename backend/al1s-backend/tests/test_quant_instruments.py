from datetime import date

from sqlalchemy import create_engine, event
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


def test_name_cache_updates_existing_instrument_without_changing_identity():
    engine=create_engine('sqlite://')
    QuantInstrumentRow.__table__.create(engine)
    repo=QuantInstruments(sessionmaker(engine,expire_on_commit=False))
    instrument=repo.register('SZ','300750',date(2026,10,29))
    named=repo.set_name(instrument,'宁德时代')
    again=repo.register('SZ','300750',date(2026,10,29))
    assert named.id==instrument.id==again.id and again.name=='宁德时代'


def test_batch_name_cache_uses_one_update_and_preserves_an_existing_name():
    engine = create_engine('sqlite://')
    QuantInstrumentRow.__table__.create(engine)
    repo = QuantInstruments(sessionmaker(engine, expire_on_commit=False))
    first = repo.register('SZ', '300750', date(2026, 10, 29))
    second = repo.register('SH', '600000', date(2026, 10, 29))
    repo.set_name(first, '已核验名称')
    updates = []
    def count(_conn, _cursor, sql, *_args):
        if sql.lstrip().lower().startswith('update'):
            updates.append(sql)
    event.listen(engine, 'before_cursor_execute', count)
    try:
        repo.cache_names({('SZ', '300750'): '不应覆盖', ('SH', '600000'): '浦发银行'})
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    assert len(updates) == 1
    assert repo.register('SZ', '300750', date(2026, 10, 29)).name == '已核验名称'
    assert repo.register('SH', '600000', date(2026, 10, 29)).name == '浦发银行'
    assert first.id != second.id
