import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from al1s.bot_ai.contracts import AiEnvelope
from al1s.bot_ai.tools import BotAiTools
from al1s.freqtrade.contracts import FreqtradeSettings
from al1s.freqtrade.service import authorized_ai_source
from al1s.meme_workshop.contracts import CommandSource
from al1s.quant.contracts import QuantSettings
from al1s.quant.gateway import QuantGateway


def source(kind, target, sender='200'):
    return CommandSource(message_id='1', scope_kind=kind, target_id=target, sender_id=sender)


def test_read_boundaries_enforce_group_and_private_owner():
    service = uuid4()
    settings = QuantSettings(enabled=True, event_ws_url='ws://localhost:3001', groups=['100'], private_enabled=True)
    gateway = QuantGateway()
    gateway._snapshots[service] = SimpleNamespace(enabled=True, private_enabled=True,
        groups=frozenset({'100'}), settings=settings)
    assert gateway.read_settings(service, source('qq_group', '100')) is not None
    assert gateway.read_settings(service, source('qq_group', '101')) is None
    assert gateway.read_settings(service, source('qq_private', '200')) is not None
    assert gateway.read_settings(service, source('qq_private', '201')) is None
    freq = FreqtradeSettings(enabled=True, event_ws_url='ws://localhost:3001',
                             groups=['100'], private_enabled=True, futures_enabled=False)
    assert authorized_ai_source(freq, source('qq_group','100'), 'spot')
    assert not authorized_ai_source(freq, source('qq_group','101'), 'spot')
    assert not authorized_ai_source(freq, source('qq_private','201'), 'spot')
    assert not authorized_ai_source(freq, source('qq_group','100'), 'futures')


def test_ai_read_uses_authenticated_sender_not_model_supplied_identity(tmp_path):
    async def run():
        service = uuid4()
        item = AiEnvelope(request_id=uuid4(), service_id=service, config_version=1,
            source=source('qq_group','100'), self_id='300', sender_name='测试',
            parts=[{'type':'text','text':'查询'}], deadline=datetime.now(UTC)+timedelta(seconds=30))
        class Repo:
            def allowed(self, *_args): return True
        class Hub:
            def is_active(self, *_args): return True
        class Quant:
            def query_for_ai(self, identity, request):
                assert identity == service and request.source.sender_id == '200'
                return {'status':'success'}
        tools = BotAiTools(Repo(), Hub(), Quant(), None, tmp_path, None)
        result = await tools.execute(item, 'call-1', 'query_trading', {'market':'ashare','kind':'balance'})
        assert result['status'] == 'success'
        denied = await tools.execute(item, 'call-2', 'query_trading',
                                     {'market':'ashare','kind':'balance','user_id':'999'})
        assert denied['status'] == 'denied'
    asyncio.run(run())
