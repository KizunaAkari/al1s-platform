from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from al1s.adapters.postgres.quant_settings import QuantSettingsRecord
from al1s.freqtrade.contracts import DEFAULT_COMMANDS as FREQ_WORDS, FreqtradeReadRequest, FreqtradeRecord, FreqtradeSettings
from al1s.freqtrade.runtime import create_app as create_freqtrade_app
from al1s.freqtrade.service import FreqtradeService
from al1s.quant.commands import DEFAULT_COMMANDS as QUANT_WORDS
from al1s.quant.contracts import QuantSettings
from al1s.quant.process import create_process_app
from al1s.trading_admin.contracts import AdminTradingSource, TradingActionWrite
from al1s.trading_admin.service import TradingAdminService


@pytest.mark.parametrize(('market', 'data', 'expected'), [
    ('a-share', {'action': 'open'}, ()),
    ('a-share', {'action': 'buy', 'symbol': '600000', 'quantity': 100}, ('600000', '100')),
    ('spot', {'action': 'buy', 'symbol': 'BTC', 'amount': '12.5'}, ('BTC', '12.5')),
    ('spot', {'action': 'sell', 'symbol': 'ETH', 'all': True}, ('ETH', '全部')),
    ('spot', {'action': 'deposit', 'amount': '100'}, ('100',)),
    ('futures', {'action': 'long', 'symbol': 'SOL', 'amount': '20', 'leverage': 3}, ('SOL', '20', '3')),
    ('futures', {'action': 'close', 'symbol': 'BTC', 'all': True}, ('BTC', '全部')),
    ('futures', {'action': 'margin', 'symbol': 'ETH', 'amount': '15'}, ('ETH', '15')),
])
def test_actions_use_existing_market_units(market, data, expected):
    assert TradingActionWrite(owner_version=1, **data).arguments(market) == expected


@pytest.mark.parametrize(('market', 'data'), [
    ('a-share', {'action': 'buy', 'symbol': 'BTC', 'quantity': 100}),
    ('a-share', {'action': 'buy', 'symbol': '600000', 'quantity': 0}),
    ('spot', {'action': 'buy', 'symbol': 'BTC', 'all': True}),
    ('spot', {'action': 'buy', 'symbol': 'DOGE', 'amount': '1'}),
    ('spot', {'action': 'buy', 'symbol': 'BTC', 'amount': 'NaN'}),
    ('spot', {'action': 'deposit', 'amount': '1.00001'}),
    ('futures', {'action': 'margin', 'symbol': 'BTC', 'amount': '1000000001'}),
    ('futures', {'action': 'long', 'symbol': 'BTC', 'amount': '5', 'leverage': 151}),
    ('futures', {'action': 'close', 'symbol': 'BTC', 'all': True, 'amount': '1'}),
])
def test_actions_reject_invalid_or_cross_market_inputs(market, data):
    with pytest.raises(ValueError):
        TradingActionWrite(owner_version=1, **data).arguments(market)


def test_browser_cannot_choose_another_owner_in_action():
    with pytest.raises(ValidationError):
        TradingActionWrite(owner_version=1, action='buy', symbol='BTC', amount='10', qq_id='999')


def test_quant_admin_source_only_works_on_the_protected_admin_route():
    handler = MagicMock(return_value='已受理')
    app = create_process_app('x' * 32, command_handler=handler)
    client = TestClient(app)
    service = uuid4()
    settings = QuantSettings(enabled=True, event_ws_url='ws://llbot:3001', groups=[])
    record = QuantSettingsRecord(settings=settings, commands=QUANT_WORDS, row_version=1, mapping_version=1)
    headers = {'X-Quant-Control': 'x' * 32}
    assert client.put(f'/internal/config/{service}', json=record.model_dump(mode='json'), headers=headers).status_code == 200
    body = {'source': {'scope_kind': 'admin', 'sender_id': '123', 'target_id': '123', 'message_id': 'a' * 32},
            'action': 'buy', 'arguments': ['600000', '100'], 'mapping_version': 1, 'settings_version': 1,
            'event_timestamp': int(datetime.now(UTC).timestamp()), 'normalized_keyword': QUANT_WORDS['buy'],
            'settings': settings.model_dump(mode='json'), 'commands': QUANT_WORDS}
    assert client.post(f'/internal/admin/command/{service}', json=body).status_code == 401
    assert client.post(f'/internal/command/{service}', json=body, headers=headers).status_code == 422
    assert client.post(f'/internal/admin/command/{service}', json=body, headers=headers).json()['content'] == '已受理'
    assert handler.call_count == 1 and handler.call_args.args[1].source.scope_kind == 'admin'


def test_freqtrade_admin_read_requires_existing_native_data_and_does_not_expand_qq_access():
    repo, manager = MagicMock(), MagicMock()
    service_id = uuid4()
    record = FreqtradeRecord(row_version=1, settings=FreqtradeSettings(enabled=True, futures_enabled=True,
        event_ws_url='ws://llbot:3001'), commands=FREQ_WORDS)
    repo.get.return_value = record
    repo.profile.return_value = SimpleNamespace(id=uuid4(), service_id=service_id, state='ready')
    source = AdminTradingSource(sender_id='123', target_id='123', message_id='a' * 32)
    query = FreqtradeReadRequest(source=source, market='futures', kind='balance')
    service = FreqtradeService(repo, manager)
    assert service.query_for_ai(service_id, query)['status'] == 'denied'
    manager.has_namespace.return_value = False
    assert service.query_for_ai(service_id, query, admin=True)['code'] == 'account_data_missing'
    manager.ensure.assert_not_called()
    manager.has_namespace.return_value = True
    native = manager.ensure.return_value
    native.capital_version = 0
    native.call.side_effect = lambda path: {'currencies': []} if path == 'balance' else []
    result = service.query_for_ai(service_id, query, admin=True)
    assert result['status'] == 'success' and result['market'] == 'futures'


def test_freqtrade_admin_source_is_rejected_by_qq_route_and_requires_control_token():
    repo, manager = MagicMock(), MagicMock()
    service_id = uuid4()
    record = FreqtradeRecord(row_version=1, settings=FreqtradeSettings(enabled=True, futures_enabled=True,
        event_ws_url='ws://llbot:3001'), commands=FREQ_WORDS)
    repo.get.return_value = record
    client = TestClient(create_freqtrade_app(repo, manager, 'x' * 32))
    headers = {'X-Freqtrade-Control': 'x' * 32}
    assert client.put(f'/config/{service_id}', json=record.model_dump(mode='json'), headers=headers).status_code == 200
    body = {'source': {'scope_kind': 'admin', 'sender_id': '123', 'target_id': '123', 'message_id': 'a' * 32},
            'action': 'buy', 'arguments': ['BTC', '10'], 'event_timestamp': int(datetime.now(UTC).timestamp()),
            'settings_version': 1, 'mapping_version': 1, 'normalized_keyword': FREQ_WORDS['buy'],
            'settings': record.settings.model_dump(mode='json'), 'commands': FREQ_WORDS}
    assert client.post(f'/admin/command/{service_id}', json=body).status_code == 401
    assert client.post(f'/command/{service_id}', json=body, headers=headers).status_code == 503
    with patch.object(FreqtradeService, 'execute', return_value='已受理') as execute:
        assert client.post(f'/admin/command/{service_id}', json=body, headers=headers).json()['content'] == '已受理'
        assert execute.call_args.args[1].source.scope_kind == 'admin'


def test_admin_routes_reject_unbound_owner_fields_and_invalid_markets():
    from fastapi import FastAPI
    from al1s.api.trading_admin import router
    app = FastAPI()
    app.include_router(router)
    app.state.trading_admin = MagicMock()
    app.state.trading_admin_repository = MagicMock()
    client = TestClient(app)
    service = uuid4()
    headers = {'Idempotency-Key': str(uuid4())}
    assert client.post(f'/bots/trading/{service}/spot/actions', headers=headers,
                       json={'owner_version': 1, 'action': 'buy', 'symbol': 'BTC', 'amount': '10', 'qq_id': '999'}).status_code == 422
    assert client.post(f'/bots/trading/{service}/real/actions', headers=headers,
                       json={'owner_version': 1, 'action': 'buy', 'symbol': 'BTC', 'amount': '10'}).status_code == 422
    assert client.post(f'/bots/trading/{service}/spot/actions', headers=headers,
                       json={'action': 'buy', 'symbol': 'BTC', 'amount': '10'}).status_code == 422
    assert client.put('/bots/trading/account-owner', json={'qq_id': '123'}).status_code == 422
    app.state.trading_admin.submit.assert_not_called()


def test_platform_proxy_passes_only_expected_trading_parameters_and_idempotency():
    import asyncio
    import httpx
    from fastapi import FastAPI
    from al1s.api.meme_proxy import router
    from al1s.meme_workshop.transport import BackendResponse
    async def run():
        app = FastAPI()
        backend = MagicMock()
        backend.forward.return_value = BackendResponse(200, b'{}', 'application/json')
        app.state.services = SimpleNamespace(meme_backends={'qq': backend})
        app.state.meme_backends = {'qq': backend}
        app.include_router(router, prefix='/api/v1')
        service, key = uuid4(), str(uuid4())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get(f'/api/v1/bots/trading/{service}/spot/orders?page=2')).status_code == 200
            assert backend.forward.call_args.args[1].endswith('/spot/orders?page=2')
            assert (await client.get(f'/api/v1/bots/trading/{service}/spot/orders?qq_id=999')).status_code == 400
            assert (await client.post(f'/api/v1/bots/trading/{service}/spot/actions', headers={'Idempotency-Key': key},
                                      json={'action': 'buy', 'symbol': 'BTC', 'amount': '10'})).status_code == 200
            assert backend.forward.call_args.args[3]['idempotency-key'] == key
    asyncio.run(run())


def test_admin_submit_uses_bound_owner_and_does_not_repeat_an_existing_receipt():
    repo, quant, gateway, process, freq_repo, freq_client = [MagicMock() for _ in range(6)]
    service_id, operation_id = uuid4(), uuid4()
    settings = QuantSettings(enabled=True, event_ws_url='ws://llbot:3001')
    record = QuantSettingsRecord(settings=settings, commands=QUANT_WORDS, row_version=2, mapping_version=3)
    quant.get.return_value = record
    gateway.applied_versions.return_value = process.applied_versions.return_value = (2, 3)
    operation = {'id': str(operation_id), 'service_id': str(service_id), 'qq_id': '123', 'market': 'a-share',
                 'action': 'buy', 'status': 'running', 'event_time': int(datetime.now(UTC).timestamp()), 'result': None}
    repo.claim.side_effect = [(operation.copy(), True), (operation.copy(), False)]
    repo.start_dispatch.return_value = True
    repo.operation.return_value = {**operation, 'status': 'completed', 'result': '已受理'}
    process.execute_admin.return_value = '已受理'
    service = TradingAdminService(repo, quant, gateway, process, freq_repo, freq_client)
    action = TradingActionWrite(owner_version=1, action='buy', symbol='600000', quantity=100)
    assert service.submit(service_id, 'a-share', action, 'same-key')['status'] == 'completed'
    assert service.submit(service_id, 'a-share', action, 'same-key')['status'] == 'running'
    command = process.execute_admin.call_args.args[1]
    assert command.source.sender_id == '123' and command.source.scope_kind == 'admin'
    assert process.execute_admin.call_count == 1


def test_admin_submit_marks_timed_out_request_unknown():
    repo, quant, gateway, process, freq_repo, freq_client = [MagicMock() for _ in range(6)]
    service_id, operation_id = uuid4(), uuid4()
    record = QuantSettingsRecord(settings=QuantSettings(enabled=True, event_ws_url='ws://llbot:3001'),
                                 commands=QUANT_WORDS, row_version=1, mapping_version=1)
    quant.get.return_value = record
    gateway.applied_versions.return_value = process.applied_versions.return_value = (1, 1)
    operation = {'id': str(operation_id), 'service_id': str(service_id), 'qq_id': '123', 'market': 'a-share',
                 'action': 'buy', 'status': 'running', 'event_time': int(datetime.now(UTC).timestamp()), 'result': None}
    repo.claim.return_value = operation, True
    repo.start_dispatch.return_value = True
    repo.operation.return_value = {**operation, 'status': 'unknown', 'result': '待核实'}
    process.execute_admin.side_effect = TimeoutError()
    service = TradingAdminService(repo, quant, gateway, process, freq_repo, freq_client)
    assert service.submit(service_id, 'a-share', TradingActionWrite(owner_version=1, action='buy', symbol='600000', quantity=100), 'key')['status'] == 'unknown'
    assert repo.finish.call_args.args[1] == 'unknown'


def test_admin_read_discards_results_if_binding_changes_during_query():
    repo, quant, gateway, process, freq_repo, freq_client = [MagicMock() for _ in range(6)]
    service_id = uuid4()
    record = QuantSettingsRecord(settings=QuantSettings(enabled=True, event_ws_url='ws://llbot:3001'),
                                 commands=QUANT_WORDS, row_version=1, mapping_version=1)
    repo.owner_for.side_effect = [('123', 1), ('456', 2)]
    quant.get.return_value = record
    gateway.applied_versions.return_value = process.applied_versions.return_value = (1, 1)
    process.query_admin.return_value = {'status': 'success', 'data': {'cash': '123'}}
    repo.pending.return_value = None
    controller = TradingAdminService(repo, quant, gateway, process, freq_repo, freq_client)
    with pytest.raises(ValueError, match='trading_owner_changed'):
        controller.query(service_id, 'a-share', 'balance', 1)


@pytest.mark.parametrize(('market', 'body', 'expected_action', 'arguments'), [
    ('spot', {'action': 'open'}, 'open', ()),
    ('spot', {'action': 'sell', 'symbol': 'BTC', 'all': True}, 'sell', ('BTC', '全部')),
    ('spot', {'action': 'deposit', 'amount': '20'}, 'deposit', ('20',)),
    ('futures', {'action': 'open'}, 'f_open', ()),
    ('futures', {'action': 'long', 'symbol': 'ETH', 'amount': '10', 'leverage': 3}, 'long', ('ETH', '10', '3')),
    ('futures', {'action': 'short', 'symbol': 'SOL', 'amount': '10'}, 'short', ('SOL', '10')),
    ('futures', {'action': 'close', 'symbol': 'BTC', 'all': True}, 'close', ('BTC', '全部')),
    ('futures', {'action': 'deposit', 'amount': '20'}, 'f_deposit', ('20',)),
    ('futures', {'action': 'margin', 'symbol': 'ETH', 'amount': '5'}, 'f_margin', ('ETH', '5')),
])
def test_crypto_admin_actions_map_to_existing_engine_operations(market, body, expected_action, arguments):
    repo, quant, gateway, process, freq_repo, freq_client = [MagicMock() for _ in range(6)]
    service_id, operation_id = uuid4(), uuid4()
    record = FreqtradeRecord(settings=FreqtradeSettings(enabled=True, futures_enabled=True,
        event_ws_url='ws://llbot:3001'), commands=FREQ_WORDS, row_version=1)
    freq_repo.get.return_value = record
    freq_client.applied.return_value = 1
    operation = {'id': str(operation_id), 'service_id': str(service_id), 'qq_id': '123', 'market': market,
                 'action': body['action'], 'status': 'running', 'event_time': int(datetime.now(UTC).timestamp()), 'result': None}
    repo.claim.return_value = operation, True
    repo.start_dispatch.return_value = True
    repo.freqtrade_status.return_value = 'completed'
    repo.operation.return_value = {**operation, 'status': 'completed', 'result': '已处理'}
    freq_client.execute_admin.return_value = '已处理'
    controller = TradingAdminService(repo, quant, gateway, process, freq_repo, freq_client)
    assert controller.submit(service_id, market, TradingActionWrite(owner_version=1, **body), str(uuid4()))['status'] == 'completed'
    command = freq_client.execute_admin.call_args.args[1]
    assert command.action == expected_action and command.arguments == arguments
    assert command.source.scope_kind == 'admin' and command.source.sender_id == '123'
