import asyncio
import json

import pytest

from al1s.bot_ai.agent import run_agent


class Provider:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    async def generate(self, system, messages, tools):
        self.requests.append((system, list(messages), tools))
        return self.replies.pop(0)


class Invocation:
    def __init__(self):
        self.calls = []

    async def tool(self, identity, name, arguments):
        self.calls.append((identity, name, arguments))
        return {'status': 'success', 'price': '1'}


def call(identity='call-1', name='market_quote', arguments='{"market":"ashare","symbol":"600000"}'):
    return {'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


def test_agent_returns_paired_tool_history_and_uses_only_authorized_tools():
    async def run():
        model = Provider([{'text': '', 'tool_calls': [call()]}, {'text': '结果', 'tool_calls': []}])
        invocation = Invocation()
        current = {'role': 'user', 'content': [{'type': 'text', 'text': '查询'}]}
        text, stored, attachments = await run_agent(model, 'persona', [], current, invocation, ())
        assert text == '结果' and attachments == [] and stored[0] == current
        assert stored[1]['tool_calls'][0]['id'] == stored[2]['tool_call_id'] == 'call-1'
        assert stored[2]['name'] == 'market_quote'
        assert json.loads(stored[2]['content'])['status'] == 'success'
        assert invocation.calls == [('call-1', 'market_quote', {'market': 'ashare', 'symbol': '600000'})]
        assert not any(tool['name'] in {'shell', 'trade', 'exec'} for tool in model.requests[0][2])
    asyncio.run(run())


def test_agent_preserves_responses_reasoning_in_legacy_compatible_history():
    async def run():
        reasoning = {'type':'reasoning','id':'r1','summary':[],'encrypted_content':'opaque'}
        model = Provider([{'text':'','tool_calls':[call()], 'reasoning_items':[reasoning]},
                          {'text':'好','tool_calls':[]}])
        _, stored, _ = await run_agent(model, 'persona', [], {'role':'user','content':'x'}, Invocation(), ())
        state = json.loads(stored[1]['content'][0]['encrypted'])
        assert state == {'type':'openai_responses_reasoning','items':[reasoning]}
    asyncio.run(run())


def test_agent_preserves_gemini_text_signature_in_legacy_history():
    async def run():
        model = Provider([{'text':'好','tool_calls':[], 'thinking_signature':'dGVzdA=='}])
        _, stored, _ = await run_agent(model, 'persona', [], {'role':'user','content':'x'}, Invocation(), ())
        assert stored[1]['content'][0] == {'type':'think','think':'','encrypted':'dGVzdA=='}
        assert stored[1]['content'][1] == {'type':'text','text':'好'}
    asyncio.run(run())


def test_invalid_or_unknown_tool_returns_denial_without_calling_backend():
    async def run():
        model = Provider([{'text': '', 'tool_calls': [call(name='shell', arguments='{}')]},
                          {'text': '无法执行', 'tool_calls': []}])
        invocation = Invocation()
        _, stored, _ = await run_agent(model, 'persona', [], {'role': 'user', 'content': 'x'}, invocation, ())
        assert invocation.calls == []
        assert json.loads(stored[2]['content'])['status'] == 'denied'
    asyncio.run(run())


def test_tool_budget_rejects_large_batch_before_any_effect():
    async def run():
        model = Provider([{'text': '', 'tool_calls': [call(str(i)) for i in range(5)]}])
        invocation = Invocation()
        with pytest.raises(ValueError, match='tool_limit'):
            await run_agent(model, 'persona', [], {'role': 'user', 'content': 'x'}, invocation, ())
        assert invocation.calls == []
    asyncio.run(run())


def test_meme_targets_resolve_only_to_actual_mentions():
    async def run():
        model = Provider([{'text': '', 'tool_calls': [call(name='send_meme',
                arguments='{"template_key":"摸摸","target":"mention1"}')]},
                          {'text': '好', 'tool_calls': []}])
        invocation = Invocation()
        await run_agent(model, 'persona', [], {'role': 'user', 'content': 'x'}, invocation, ('222',))
        assert invocation.calls[0][2]['target'] == '222'
    asyncio.run(run())


def test_sticker_tool_only_stages_a_candidate_without_sending():
    async def run():
        model = Provider([
            {'text': '', 'tool_calls': [call('s1', 'search_sticker', '{"query":"开心"}')]},
            {'text': '', 'tool_calls': [call('s2', 'send_sticker', '{"sticker_id":"1"}')]},
            {'text': '好', 'tool_calls': []},
        ])
        invocation = Invocation()
        class Stickers:
            staged = None
            async def search(self, query):
                assert query == '开心'
                return [{'id': 'a' * 64, 'index': 1}]
            async def stage(self, identity):
                assert identity == 'a' * 64
                self.staged = '/request/sticker.gif'
                return self.staged
        stickers = Stickers()
        text, stored, attachments = await run_agent(model, 'persona', [],
            {'role': 'user', 'content': 'x'}, invocation, (), stickers=stickers)
        assert text == '好' and attachments == ['/request/sticker.gif'] and invocation.calls == []
        assert json.loads(stored[4]['content'])['status'] == 'staged'
        assert any(tool['name'] == 'search_sticker' for tool in model.requests[0][2])
    asyncio.run(run())


def test_sticker_tool_rejects_unoffered_candidate():
    async def run():
        model = Provider([{'text': '', 'tool_calls': [call('s1', 'send_sticker', '{"sticker_id":"1"}')]},
                          {'text': '无表情', 'tool_calls': []}])
        class Stickers:
            async def search(self, query): return []
            async def stage(self, identity): raise AssertionError('must not stage')
        _, stored, attachments = await run_agent(model, 'persona', [], {'role':'user','content':'x'},
                                                 Invocation(), (), stickers=Stickers())
        assert json.loads(stored[2]['content'])['status'] == 'denied' and attachments == []
    asyncio.run(run())
