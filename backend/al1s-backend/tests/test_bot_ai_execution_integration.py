import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from al1s.bot_ai.contracts import AiEnvelope
from al1s.bot_ai.executor import AiExecutor
from al1s.bot_ai.local_runtime import LocalAiRuntime
from al1s.meme_workshop.contracts import CommandSource


def test_local_executor_runs_tool_persists_paired_history_then_sends(tmp_path):
    async def run():
        service, profile = uuid4(), uuid4()
        item = AiEnvelope(request_id=uuid4(), service_id=service, config_version=2,
            source=CommandSource(message_id='1', scope_kind='qq_group', target_id='100', sender_id='200'),
            self_id='300', sender_name='测试', parts=[{'type':'text','text':'行情'}],
            chat_profile_id=profile, persona_prompt='persona', deadline=datetime.now(UTC)+timedelta(seconds=30))
        class History:
            saved = []
            def context(self, *_args): return SimpleNamespace(messages=[])
            def append(self, _context, identity, messages, **guard): self.saved.append((identity,messages,guard))
        class Model:
            calls = 0
            async def generate(self, _system, _messages, _tools):
                self.calls += 1
                if self.calls == 1:
                    return {'text':'', 'tool_calls':[{'id':'call-1','type':'function',
                        'function':{'name':'market_quote','arguments':'{"market":"ashare","symbol":"600000"}'}}]}
                return {'text':'结果', 'tool_calls':[]}
        history, sent, complete = History(), [], []
        done = asyncio.Event()
        async def tool(source, _call, name, arguments):
            assert source.source.sender_id == '200' and name == 'market_quote' and arguments['symbol'] == '600000'
            return {'status':'success', 'price':'1'}
        async def output(source, sequence, parts):
            assert history.saved and source.request_id == item.request_id
            sent.append((sequence,parts))
            return {'status':'sent'}
        async def finished(_item, status, *rest):
            complete.append(status)
            done.set()
        runtime = LocalAiRuntime()
        runtime.set_handlers(on_tool=tool, on_output=output, on_completed=finished)
        executor = AiExecutor({profile:SimpleNamespace(provider=Model(), supports_vision=False)}, history, tmp_path)
        await runtime.activate(service, 2, executor)
        await runtime.dispatch(item)
        await asyncio.wait_for(done.wait(), 3)
        assert complete == ['completed'] and sent == [(0,[{'type':'text','text':'结果'}])]
        assert history.saved[0][1][1]['tool_calls'][0]['id'] == history.saved[0][1][2]['tool_call_id'] == 'call-1'
        assert history.saved[0][2]['epoch'] and history.saved[0][2]['config_version'] == 2
        await runtime.close()
    asyncio.run(run())
