import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres import notification_models  # Registers shared foreign keys.
from al1s.adapters.postgres.bot_models import BotServiceRow
from al1s.adapters.postgres.freqtrade_models import FreqtradeFuturesProfileRow, FreqtradeProfileRow, FreqtradeRequestRow
from al1s.adapters.postgres.freqtrade_repository import FreqtradeRepository
from al1s.adapters.postgres.quant_accounts import QuantAccounts
from al1s.adapters.postgres.quant_instruments import QuantInstruments
from al1s.adapters.postgres.quant_models import QuantAccountRow, QuantInstrumentRow, QuantOrderRow, QuantPositionRow, QuantReplyRow
from al1s.adapters.postgres.quant_orders import QuantOrders
from al1s.adapters.postgres.quant_settings import QuantSettingsRecord
from al1s.adapters.postgres.trading_admin_models import TradingAdminOperationRow
from al1s.adapters.postgres.trading_admin_repository import TradingAdminRepository
from al1s.freqtrade.contracts import FreqtradeSettings
from al1s.freqtrade.service import FreqtradeService
from al1s.quant.commands import DEFAULT_COMMANDS
from al1s.quant.commands import QuantCommand
from al1s.quant.contracts import QuantSettings
from al1s.quant.service import QuantService
from al1s.trading_admin.contracts import AdminTradingSource, TradingActionWrite
from al1s.trading_admin.service import TradingAdminService

pytestmark = pytest.mark.integration


@pytest.fixture
def sessions():
    url = os.environ.get('AL1S_TEST_DATABASE_URL')
    if not url:
        pytest.skip('isolated PostgreSQL required')
    name = make_url(url).database or ''
    if not name.startswith('al1s_comfy_test_'):
        raise RuntimeError('trading tests require a dedicated al1s_comfy_test_ database')
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text('TRUNCATE TABLE trading_admin_operations, trading_admin_owner, quant_accounts, quant_instruments, bot_services CASCADE'))
    yield sessionmaker(engine, expire_on_commit=False)
    with engine.begin() as connection:
        connection.execute(text('TRUNCATE TABLE trading_admin_operations, trading_admin_owner, quant_accounts, quant_instruments, bot_services CASCADE'))
    engine.dispose()


def setup(sessions):
    with sessions.begin() as session:
        service = BotServiceRow(id=uuid4(), name='synthetic-qq', kind='onebot_gateway', enabled=True)
        session.add(service)
        service_id = service.id
    repo = TradingAdminRepository(sessions)
    assert repo.set_owner('123', 0) == {'qq_id': '123', 'row_version': 1}
    return service_id, repo


def test_binding_and_idempotent_operations_are_isolated(sessions):
    service, repo = setup(sessions)
    assert repo.set_owner('123', 0)['row_version'] == 1
    with pytest.raises(ValueError, match='trading_owner_conflict'):
        repo.set_owner('456', 0)
    key = str(uuid4())
    first, created = repo.claim(service, 'a-share', 'buy', ('600000', '100'), key, 1)
    replay, again = repo.claim(service, 'a-share', 'buy', ('600000', '100'), key, 1)
    assert created and not again and first['id'] == replay['id'] == key
    with pytest.raises(ValueError, match='idempotency_conflict'):
        repo.claim(service, 'a-share', 'buy', ('600000', '200'), key, 1)
    with pytest.raises(ValueError, match='trading_operation_pending'):
        repo.claim(service, 'a-share', 'sell', ('600000', '100'), str(uuid4()), 1)
    with pytest.raises(ValueError, match='trading_owner_busy'):
        repo.set_owner('456', 1)
    assert repo.finish(UUID(key), 'completed', '已受理')
    assert repo.set_owner('456', 1) == {'qq_id': '456', 'row_version': 2}
    with pytest.raises(ValueError, match='trading_owner_changed'):
        repo.claim(service, 'a-share', 'buy', ('600000', '100'), str(uuid4()), 1)
    with pytest.raises(ValueError, match='trading_operation_not_found'):
        repo.operation(service, 'a-share', UUID(key))


def test_concurrent_duplicate_requests_create_one_receipt(sessions):
    service, repo = setup(sessions)
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repo.claim(service, 'spot', 'buy', ('BTC', '20'), key, 1), range(2)))
    assert sorted(created for _, created in results) == [False, True]
    assert {row['id'] for row, _ in results} == {key}


def test_uncertain_stock_submission_protects_account_and_does_not_repeat(sessions):
    service, repo = setup(sessions)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(QuantAccountRow(id=uuid4(), qq_user_id='123', initial_cash=Decimal('1000'),
            cash=Decimal('1000'), state='active', version=1, created_at=now, updated_at=now))
    key = str(uuid4())
    repo.claim(service, 'a-share', 'buy', ('600000', '100'), key, 1)
    assert repo.start_dispatch(UUID(key))
    assert repo.finish(UUID(key), 'unknown', '待核实')
    with sessions() as session:
        account = session.query(QuantAccountRow).filter_by(qq_user_id='123').one()
        assert account.state == 'needs_review' and account.version == 2
    assert repo.operation(service, 'a-share', UUID(key))['status'] == 'unknown'
    with pytest.raises(ValueError, match='trading_operation_pending'):
        repo.claim(service, 'a-share', 'buy', ('600000', '100'), str(uuid4()), 1)


def test_completed_native_receipt_can_safely_clear_its_own_crypto_protection(sessions):
    service, repo = setup(sessions)
    with sessions.begin() as session:
        session.add(FreqtradeProfileRow(id=uuid4(), qq_id='123', service_id=service,
            initial_cash=Decimal('1000'), state='ready', native_active=False, created_at=datetime.now(UTC)))
    key = str(uuid4())
    operation, _ = repo.claim(service, 'spot', 'buy', ('BTC', '20'), key, 1)
    assert repo.start_dispatch(UUID(key)) and repo.finish(UUID(key), 'unknown', '待核实')
    with sessions.begin() as session:
        account = session.query(FreqtradeProfileRow).filter_by(qq_id='123').one()
        assert account.state == 'unknown'
        session.add(FreqtradeRequestRow(id=uuid4(), service_id=service, scope_kind='admin', target_id='123',
            sender_id='123', message_id=UUID(key).hex, event_time=operation['event_time'], action='buy', market='spot',
            request_sha256='a' * 64, status='completed', result='已受理', created_at=datetime.now(UTC)))
    assert repo.operation(service, 'spot', UUID(key))['status'] == 'completed'
    with sessions() as session:
        assert session.query(FreqtradeProfileRow).filter_by(qq_id='123').one().state == 'ready'


def test_reconciliation_does_not_clear_a_preexisting_crypto_unknown_state(sessions):
    service, repo = setup(sessions)
    with sessions.begin() as session:
        session.add(FreqtradeProfileRow(id=uuid4(), qq_id='123', service_id=service,
            initial_cash=Decimal('1000'), state='unknown', native_active=False, created_at=datetime.now(UTC)))
    key = str(uuid4())
    operation, _ = repo.claim(service, 'spot', 'buy', ('BTC', '20'), key, 1)
    assert repo.start_dispatch(UUID(key)) and repo.finish(UUID(key), 'unknown', '待核实')
    with sessions.begin() as session:
        session.add(FreqtradeRequestRow(id=uuid4(), service_id=service, scope_kind='admin', target_id='123',
            sender_id='123', message_id=UUID(key).hex, event_time=operation['event_time'], action='buy', market='spot',
            request_sha256='a' * 64, status='completed', result='已受理', created_at=datetime.now(UTC)))
    assert repo.operation(service, 'spot', UUID(key))['status'] == 'completed'
    with sessions() as session:
        assert session.query(FreqtradeProfileRow).filter_by(qq_id='123').one().state == 'unknown'


@pytest.mark.parametrize('changed', [False, True])
def test_stock_reconciliation_clears_only_its_unchanged_protection(sessions, changed):
    service, repo = setup(sessions)
    now, instrument_id = datetime.now(UTC), uuid4()
    with sessions.begin() as session:
        session.add(QuantAccountRow(id=uuid4(), qq_user_id='123', initial_cash=Decimal('1000'),
            cash=Decimal('1000'), state='active', version=1, created_at=now, updated_at=now))
        session.add(QuantInstrumentRow(id=instrument_id, market='SH', current_code='600000', board='main',
                                       name='合成股票', active=True))
    key = str(uuid4())
    operation, _ = repo.claim(service, 'a-share', 'buy', ('600000', '100'), key, 1)
    source = AdminTradingSource(sender_id='123', target_id='123', message_id=UUID(key).hex)
    command = QuantCommand(source, 'buy', ('600000', '100'), 1, operation['event_time'],
                           settings_version=1, settings=QuantSettings())
    assert repo.start_dispatch(UUID(key))
    QuantOrders(sessions, enforce_corporate=False).accept(service, command, instrument_id, 100)
    assert repo.finish(UUID(key), 'unknown', '待核实')
    if changed:
        with sessions.begin() as session:
            account = session.scalar(select(QuantAccountRow).where(QuantAccountRow.qq_user_id == '123'))
            account.version += 1
    assert repo.operation(service, 'a-share', UUID(key))['status'] == 'completed'
    with sessions() as session:
        account = session.scalar(select(QuantAccountRow).where(QuantAccountRow.qq_user_id == '123'))
        assert account.state == ('needs_review' if changed else 'active')


def test_stock_reconciliation_keeps_receipt_until_processing_order_is_settled(sessions):
    service, repo = setup(sessions)
    now, instrument_id = datetime.now(UTC), uuid4()
    with sessions.begin() as session:
        session.add(QuantAccountRow(id=uuid4(), qq_user_id='123', initial_cash=Decimal('1000'),
            cash=Decimal('1000'), state='active', version=1, created_at=now, updated_at=now))
        session.add(QuantInstrumentRow(id=instrument_id, market='SH', current_code='600000', board='main',
                                       name='合成股票', active=True))
    key = str(uuid4())
    operation, _ = repo.claim(service, 'a-share', 'buy', ('600000', '100'), key, 1)
    source = AdminTradingSource(sender_id='123', target_id='123', message_id=UUID(key).hex)
    command = QuantCommand(source, 'buy', ('600000', '100'), 1, operation['event_time'],
                           settings_version=1, settings=QuantSettings())
    assert repo.start_dispatch(UUID(key))
    QuantOrders(sessions, enforce_corporate=False).accept(service, command, instrument_id, 100)
    assert repo.finish(UUID(key), 'unknown', '待核实')
    with sessions.begin() as session:
        order = session.scalar(select(QuantOrderRow).where(QuantOrderRow.message_id == UUID(key).hex))
        order.status = 'processing'
    assert repo.operation(service, 'a-share', UUID(key))['status'] == 'unknown'
    with sessions.begin() as session:
        account = session.scalar(select(QuantAccountRow).where(QuantAccountRow.qq_user_id == '123'))
        assert account.state == 'needs_review'
        order = session.scalar(select(QuantOrderRow).where(QuantOrderRow.message_id == UUID(key).hex))
        order.status = 'filled'
    assert repo.operation(service, 'a-share', UUID(key))['status'] == 'completed'
    with sessions() as session:
        assert session.scalar(select(QuantAccountRow).where(QuantAccountRow.qq_user_id == '123')).state == 'active'


def test_pending_query_stays_bounded_with_history(sessions):
    service, repo = setup(sessions)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add_all([TradingAdminOperationRow(id=uuid4(), service_id=service, qq_id='123', market='spot',
            action='buy', key_hash=f'{index:064x}', request_hash='a' * 64, status='completed',
            event_time=int(now.timestamp()), created_at=now, updated_at=now) for index in range(1000)])
    count = []
    engine = sessions.kw['bind']
    def record(_conn, _cursor, sql, *_args):
        if sql.lstrip().lower().startswith('select'): count.append(sql)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        assert repo.pending(service, 'spot') is None
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert len(count) == 2 and 'LIMIT' in count[-1].upper()


def test_stock_web_open_and_order_use_the_existing_account_and_order_ledger(sessions):
    service_id, repo = setup(sessions)
    instrument_id = uuid4()
    with sessions.begin() as session:
        session.add(QuantInstrumentRow(id=instrument_id, market='SH', current_code='600000', board='main',
                                       name='合成股票', active=True))
    accounts, orders = QuantAccounts(sessions), QuantOrders(sessions, enforce_corporate=False)
    failures = []
    def trade(service, command):
        try:
            order, created = orders.accept(service, command, instrument_id, int(command.arguments[1]))
        except Exception as error:
            failures.append((type(error).__name__, str(error)))
            raise
        return '已受理' if created else '原委托已存在'
    engine = QuantService(accounts, orders, trade=trade)
    settings = QuantSettings(enabled=True, event_ws_url='ws://llbot:3001')
    record = QuantSettingsRecord(settings=settings, commands=DEFAULT_COMMANDS, row_version=1, mapping_version=1)
    gateway, process, config = MagicMock(), MagicMock(), MagicMock()
    config.get.return_value = record
    gateway.applied_versions.return_value = process.applied_versions.return_value = (1, 1)
    process.execute_admin.side_effect = engine.execute
    process.query_admin.side_effect = lambda _service, request: engine.query_for_ai(request.source, settings, request.kind, request.page)
    controller = TradingAdminService(repo, config, gateway, process, None, None)
    opened = controller.submit(service_id, 'a-share', TradingActionWrite(owner_version=1, action='open'), str(uuid4()))
    assert opened['status'] == 'completed' and accounts.get('123') is not None
    key = str(uuid4())
    action = TradingActionWrite(owner_version=1, action='buy', symbol='600000', quantity=100)
    outcome = controller.submit(service_id, 'a-share', action, key)
    assert outcome['status'] == 'completed', failures
    assert controller.submit(service_id, 'a-share', action, key)['status'] == 'completed'
    with sessions() as session:
        rows = session.scalars(select(QuantOrderRow)).all()
        assert len(rows) == 1 and rows[0].scope_kind == 'admin' and rows[0].sender_id == '123'
        assert not session.scalars(select(QuantReplyRow)).all()
    assert controller.query(service_id, 'a-share', 'trades', 1)['data']['trades'][0]['quantity'] == 100


def test_stock_name_enrichment_reuses_one_cache_for_positions_and_orders(sessions):
    service_id, _ = setup(sessions)
    accounts, orders, instruments = QuantAccounts(sessions), QuantOrders(sessions, enforce_corporate=False), QuantInstruments(sessions)
    account, _ = accounts.open('123', Decimal('1000'))
    instrument = instruments.register('SZ', '300750', date(2026, 10, 29))
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(QuantPositionRow(id=uuid4(), account_id=account.id, instrument_id=instrument.id,
            quantity=100, total_cost=Decimal('100'), updated_at=now))
    admin = AdminTradingSource(sender_id='123', target_id='123', message_id=uuid4().hex)
    command = QuantCommand(admin, 'buy', ('300750', '100'), 1, int(now.timestamp()),
                           settings_version=1, settings=QuantSettings())
    orders.accept(service_id, command, instrument.id, 100)
    source = MagicMock()
    source.security_names.return_value = {('SZ', '300750'): '宁德时代'}
    engine = QuantService(accounts, orders, name_source=source, instruments=instruments)
    positions = engine.query_for_ai(admin, QuantSettings(), 'positions', 1)['data']['positions']
    history = engine.query_for_ai(admin, QuantSettings(), 'trades', 1)['data']['trades']
    assert positions[0]['name'] == history[0]['name'] == '宁德时代'
    source.security_names.assert_called_once_with([('SZ', '300750')])
    with sessions() as session:
        assert session.get(QuantInstrumentRow, instrument.id).name == '宁德时代'


def test_crypto_web_orders_use_existing_wallets_and_never_enqueue_a_group_notice(sessions):
    service_id, repo = setup(sessions)
    freq_repo = FreqtradeRepository(sessions)
    settings = FreqtradeSettings(enabled=True, event_ws_url='ws://llbot:3001', futures_enabled=True)
    record = freq_repo.save(service_id, settings, None, expected_version=0, operation_id='synthetic-settings')
    manager, native = MagicMock(), MagicMock()
    manager.ensure.return_value = native
    manager.has_namespace.return_value = True
    native.capital_version = 0
    def native_call(path, payload=None):
        if path == 'status': return []
        if path == 'forceenter': return {'trade_id': 77, 'leverage': payload.get('leverage', 1)}
        if path == 'balance': return {'currencies': [{'currency': 'USDT', 'free': 100, 'balance': 100, 'used': 0}]}
        if path.startswith('trades?'): return {'trades': []}
        raise AssertionError(path)
    native.call.side_effect = native_call
    engine = FreqtradeService(freq_repo, manager)
    client = MagicMock()
    client.applied.return_value = record.row_version
    client.execute_admin.side_effect = engine.execute
    client.query_admin.side_effect = lambda service, request: engine.query_for_ai(service, request, admin=True)
    controller = TradingAdminService(repo, None, None, None, freq_repo, client)
    for market in ('spot', 'futures'):
        assert controller.submit(service_id, market, TradingActionWrite(owner_version=1, action='open'), str(uuid4()))['status'] == 'completed'
    spot = controller.submit(service_id, 'spot', TradingActionWrite(owner_version=1, action='buy', symbol='BTC', amount='20'), str(uuid4()))
    with patch('al1s.freqtrade.futures_service.leverage_limit', return_value=10):
        future = controller.submit(service_id, 'futures', TradingActionWrite(owner_version=1, action='short', symbol='ETH', amount='15', leverage=3), str(uuid4()))
    assert spot['status'] == future['status'] == 'completed'
    assert controller.query(service_id, 'spot', 'balance', 1)['data']['currencies'][0]['currency'] == 'USDT'
    with sessions.begin() as session:
        request = session.scalar(select(FreqtradeRequestRow).where(FreqtradeRequestRow.action == 'short'))
        profile = session.scalar(select(FreqtradeFuturesProfileRow).where(FreqtradeFuturesProfileRow.qq_id == '123'))
        profile.notices_since = datetime.fromtimestamp(0, UTC)
        request_id = request.id
    trade = {'enter_tag': 'al1s-' + request_id.hex, 'is_open': False, 'has_open_orders': False,
             'trade_id': 77, 'close_timestamp': int(datetime.now(UTC).timestamp() * 1000), 'exit_reason': 'liquidation',
             'pair': 'ETH/USDT:USDT', 'is_short': True}
    freq_repo.enqueue_exits(profile, [trade])
    with sessions() as session:
        assert not session.scalars(select(QuantReplyRow)).all()
        assert session.scalar(select(FreqtradeProfileRow).where(FreqtradeProfileRow.qq_id == '123')) is not None
