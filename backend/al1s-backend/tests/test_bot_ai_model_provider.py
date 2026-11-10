import asyncio
import base64
import json
from types import SimpleNamespace

from al1s.bot_ai.model_provider import ModelProvider


TOOLS = [{'name': 'search_feeds', 'description': '查快讯',
          'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}}}]
HISTORY = [{'role': 'user', 'content': [{'type': 'text', 'text': '消息'}]}]


class FakeCall:
    def __init__(self, response):
        self.response, self.arguments = response, []

    async def create(self, **kwargs):
        self.arguments.append(kwargs)
        return self.response


def test_openai_chat_preserves_tool_identity_and_limits_request():
    async def run():
        endpoint = FakeCall({'choices': [{'message': {'content': None, 'tool_calls': [
            {'id': 'call-1', 'type': 'function', 'function': {'name': 'search_feeds',
                                                               'arguments': '{"query":"x"}'}}]}}]})
        client = SimpleNamespace(chat=SimpleNamespace(completions=endpoint))
        provider = ModelProvider('openai_chat_completion', 'model', client)
        reply = await provider.generate('system', HISTORY, TOOLS)
        assert reply['tool_calls'][0]['id'] == 'call-1' and reply['text'] == ''
        request = endpoint.arguments[0]
        assert request['messages'][0] == {'role': 'system', 'content': 'system'}
        assert request['messages'][1]['role'] == 'user' and request['tools'][0]['function']['name'] == 'search_feeds'
        assert request['parallel_tool_calls'] is False
    asyncio.run(run())


def test_openai_responses_preserves_function_call_id_and_translates_results():
    async def run():
        reasoning = {'type':'reasoning','id':'r1','summary':[], 'encrypted_content':'opaque'}
        endpoint = FakeCall({'output': [reasoning, {'type': 'function_call', 'call_id': 'call-2',
                                          'name': 'search_feeds', 'arguments': '{}'}]})
        provider = ModelProvider('openai_responses', 'model', SimpleNamespace(responses=endpoint))
        reply = await provider.generate('system', HISTORY, TOOLS)
        assert reply['tool_calls'][0]['id'] == 'call-2'
        assert reply['reasoning_items'] == [reasoning]
        assert endpoint.arguments[0]['tools'][0]['name'] == 'search_feeds'
        assert endpoint.arguments[0]['store'] is False
    asyncio.run(run())


def test_openai_responses_json_mode_includes_format_instruction_in_input():
    async def run():
        endpoint = FakeCall({'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': '{"mode":"chat"}'}]}]})
        provider = ModelProvider('openai_responses', 'model', SimpleNamespace(responses=endpoint))
        messages = [{'role': 'user', 'content': '{"text":"你好","aliases":[]}'}]
        reply = await provider.generate('仅返回 JSON。', messages, json_output=True)
        request = endpoint.arguments[0]
        assert reply['text'] == '{"mode":"chat"}'
        assert request['text']['format']['type'] == 'json_object'
        assert 'json' in json.dumps(request['input'], ensure_ascii=False).lower()
        assert any('你好' in json.dumps(item, ensure_ascii=False) for item in request['input'])
    asyncio.run(run())


def test_gemini_preserves_thought_signature_and_disables_automatic_tools():
    async def run():
        endpoint = FakeCall({'candidates': [{'content': {'parts': [
            {'function_call': {'name': 'search_feeds', 'args': {'query': 'x'}},
             'thought_signature': b'signature'}]}}]})
        client = SimpleNamespace(models=SimpleNamespace(generate_content=endpoint.create))
        provider = ModelProvider('googlegenai_chat_completion', 'gemini-test', client)
        reply = await provider.generate('system', HISTORY, TOOLS)
        assert reply['tool_calls'][0]['extra_content']['google']['thought_signature'] == base64.b64encode(b'signature').decode()
        request = endpoint.arguments[0]
        assert request['config']['automatic_function_calling']['disable'] is True
        assert request['config']['tools'][0]['function_declarations'][0]['name'] == 'search_feeds'
    asyncio.run(run())


def test_gemini_preserves_signature_on_text_without_exposing_thinking():
    async def run():
        endpoint = FakeCall({'candidates':[{'content':{'parts':[
            {'text':'secret thinking','thought':True},
            {'text':'answer','thought_signature':b'text-signature'}]}}]})
        client = SimpleNamespace(models=SimpleNamespace(generate_content=endpoint.create))
        reply = await ModelProvider('googlegenai_chat_completion','gemini-test',client).generate('system',HISTORY)
        assert reply['text'] == 'answer'
        assert reply['thinking_signature'] == base64.b64encode(b'text-signature').decode()
    asyncio.run(run())
