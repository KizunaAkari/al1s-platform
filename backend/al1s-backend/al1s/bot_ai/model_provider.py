"""Small adapter for the model interfaces already supported by QQ AI."""

import base64
import json
from uuid import uuid4

from al1s.bot_ai.model_messages import for_chat, for_gemini, for_responses


def _dict(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, 'model_dump'):
        return value.model_dump(exclude_none=True)
    raise ValueError('ai_model_response_invalid')


def _call(identity, name, arguments, signature=None):
    if not isinstance(identity, str) or not identity or not isinstance(name, str) or not name:
        raise ValueError('ai_model_tool_invalid')
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments, ensure_ascii=False, separators=(',', ':'))
    if not isinstance(arguments, str) or len(arguments) > 16384:
        raise ValueError('ai_model_tool_invalid')
    result = {'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}
    if isinstance(signature, bytes):
        result['extra_content'] = {'google': {'thought_signature': base64.b64encode(signature).decode('ascii')}}
    elif isinstance(signature, str):
        result['extra_content'] = {'google': {'thought_signature': signature}}
    return result


class ModelProvider:
    def __init__(self, kind, model, client):
        self.kind, self.model, self.client = kind, model, client

    async def generate(self, system, messages, tools=(), *, json_output=False):
        if self.kind == 'openai_chat_completion':
            return await self._chat(system, messages, tools, json_output)
        if self.kind == 'openai_responses':
            return await self._responses(system, messages, tools, json_output)
        if self.kind == 'googlegenai_chat_completion':
            return await self._gemini(system, messages, tools, json_output)
        raise ValueError('ai_provider_adapter_invalid')

    async def _chat(self, system, messages, tools, json_output):
        request = {'model': self.model,
                   'messages': [{'role': 'system', 'content': system}, *for_chat(messages)]}
        if tools:
            request['tools'] = [{'type': 'function', 'function': dict(tool)} for tool in tools]
            request['parallel_tool_calls'] = False
        if json_output:
            request['response_format'] = {'type': 'json_object'}
        response = _dict(await self.client.chat.completions.create(**request))
        choices = response.get('choices') or []
        if not choices:
            raise ValueError('ai_model_response_invalid')
        message = _dict(choices[0]).get('message') or {}
        message = _dict(message)
        text = message.get('content') or ''
        if not isinstance(text, str):
            raise ValueError('ai_model_response_invalid')
        calls = []
        for raw in message.get('tool_calls') or []:
            raw = _dict(raw)
            function = _dict(raw.get('function'))
            calls.append(_call(raw.get('id'), function.get('name'), function.get('arguments')))
        return {'text': text, 'tool_calls': calls}

    async def _responses(self, system, messages, tools, json_output):
        request = {'model': self.model, 'instructions': system, 'input': for_responses(messages), 'store': False}
        if tools:
            request['tools'] = [{'type': 'function', **dict(tool)} for tool in tools]
            request['parallel_tool_calls'] = False
        if json_output:
            request['text'] = {'format': {'type': 'json_object'}}
            request['input'].insert(0, {'role': 'developer', 'content': [
                {'type': 'input_text', 'text': 'Return a JSON object.'}]})
        response = _dict(await self.client.responses.create(**request))
        texts, calls, reasoning = [], [], []
        for item in response.get('output') or []:
            item = _dict(item)
            if item.get('type') == 'function_call':
                calls.append(_call(item.get('call_id'), item.get('name'), item.get('arguments')))
            elif item.get('type') == 'reasoning':
                reasoning.append(item)
            elif item.get('type') == 'message':
                for part in item.get('content') or []:
                    part = _dict(part)
                    if part.get('type') == 'output_text' and isinstance(part.get('text'), str):
                        texts.append(part['text'])
        return {'text': '\n'.join(texts), 'tool_calls': calls, 'reasoning_items': reasoning}

    async def _gemini(self, system, messages, tools, json_output):
        config = {'system_instruction': system, 'automatic_function_calling': {'disable': True}}
        if tools:
            config['tools'] = [{'function_declarations': [dict(tool) for tool in tools]}]
        if json_output:
            config['response_mime_type'] = 'application/json'
        if self.model.startswith(('gemini-3-', 'gemini-3.')):
            config['thinking_config'] = {'thinking_level': 'LOW'}
        response = _dict(await self.client.models.generate_content(
            model=self.model, contents=for_gemini(messages), config=config))
        candidates = response.get('candidates') or []
        if not candidates:
            raise ValueError('ai_model_response_invalid')
        content = _dict(_dict(candidates[0]).get('content') or {})
        texts, calls, thinking_signature = [], [], None
        for part in content.get('parts') or []:
            part = _dict(part)
            if not part.get('thought') and isinstance(part.get('text'), str):
                texts.append(part['text'])
            if part.get('text') is not None and part.get('thought_signature'):
                signature = part['thought_signature']
                thinking_signature = (base64.b64encode(signature).decode('ascii')
                                      if isinstance(signature, bytes) else signature)
            if part.get('function_call'):
                function = _dict(part['function_call'])
                calls.append(_call(function.get('id') or uuid4().hex, function.get('name'),
                                   function.get('args') or {}, part.get('thought_signature')))
        return {'text': '\n'.join(texts), 'tool_calls': calls, 'thinking_signature': thinking_signature}
