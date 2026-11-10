"""Prepare provider-specific copies without altering stored conversation history."""

import base64
import json
import re


def _parts(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    if not isinstance(content, list):
        raise ValueError('ai_model_content_invalid')
    result = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError('ai_model_content_invalid')
        kind = part.get('type')
        if kind == 'think':
            continue
        if kind == 'text' and isinstance(part.get('text'), str):
            result.append({'type': 'text', 'text': part['text']})
        elif kind == 'image_url' and isinstance(part.get('image_url'), dict):
            url = part['image_url'].get('url')
            if (not isinstance(url, str) or len(url) > 24 * 1024 * 1024 or
                    not re.match(r'^data:image/(?:png|jpeg|jpg|webp|gif);base64,', url)):
                raise ValueError('ai_model_image_invalid')
            result.append({'type': 'image_url', 'image_url': {'url': url}})
        else:
            raise ValueError('ai_model_content_invalid')
    return result


def _calls(message, *, signatures=False):
    result = []
    for call in message.get('tool_calls') or []:
        function = call.get('function') if isinstance(call, dict) else None
        if (not isinstance(function, dict) or not isinstance(call.get('id'), str)
                or not isinstance(function.get('name'), str)
                or not isinstance(function.get('arguments'), str)):
            raise ValueError('ai_model_tool_invalid')
        cleaned = {'id': call['id'], 'type': 'function', 'function': {
            'name': function['name'], 'arguments': function['arguments']}}
        extra = call.get('extra_content') or {}
        google = extra.get('google') if isinstance(extra, dict) else None
        signature = google.get('thought_signature') if isinstance(google, dict) else None
        if signatures and isinstance(signature, str):
            cleaned['_gemini_signature'] = signature
        result.append(cleaned)
    return result


def for_chat(messages):
    output = []
    for message in messages:
        role = message.get('role')
        if role == 'tool':
            if not isinstance(message.get('tool_call_id'), str) or not isinstance(message.get('content'), str):
                raise ValueError('ai_model_tool_invalid')
            output.append({'role': role, 'tool_call_id': message['tool_call_id'], 'content': message['content']})
        elif role in {'user', 'assistant', 'system'}:
            parts, calls = _parts(message.get('content')), _calls(message)
            item = {'role': role, 'content': parts or None}
            if calls:
                item['tool_calls'] = calls
            if parts or calls:
                output.append(item)
        else:
            raise ValueError('ai_model_role_invalid')
    return output


def for_responses(messages):
    result = []
    for original in messages:
        if original.get('role') == 'assistant' and isinstance(original.get('content'), list):
            for part in original['content']:
                if not isinstance(part, dict) or part.get('type') != 'think' or not isinstance(part.get('encrypted'), str):
                    continue
                try:
                    state = json.loads(part['encrypted'])
                except ValueError:
                    continue
                if isinstance(state, dict) and state.get('type') == 'openai_responses_reasoning':
                    items = state.get('items')
                    if isinstance(items, list):
                        result.extend(item for item in items if isinstance(item, dict) and item.get('type') == 'reasoning')
        cleaned = for_chat([original])
        if not cleaned:
            continue
        message = cleaned[0]
        role = message['role']
        if role == 'tool':
            result.append({'type': 'function_call_output', 'call_id': message['tool_call_id'],
                           'output': message['content']})
            continue
        parts = []
        for part in message['content'] or []:
            if part['type'] == 'text':
                parts.append({'type': 'output_text' if role == 'assistant' else 'input_text', 'text': part['text']})
            else:
                parts.append({'type': 'input_image', 'image_url': part['image_url']['url']})
        if parts:
            result.append({'role': role, 'content': parts})
        for call in message.get('tool_calls', []):
            result.append({'type': 'function_call', 'call_id': call['id'],
                           'name': call['function']['name'], 'arguments': call['function']['arguments']})
    return result


def for_gemini(messages):
    result, call_names = [], {}
    previous_role = None
    for message in messages:
        role = message.get('role')
        if role == 'tool':
            identity = message.get('tool_call_id')
            if identity not in call_names or not isinstance(message.get('content'), str):
                raise ValueError('ai_model_tool_orphan')
            part = {'function_response': {'name': call_names.pop(identity),
                                           'response': {'result': message['content']}}}
            if previous_role == 'tool':
                result[-1]['parts'].append(part)
            else:
                result.append({'role': 'user', 'parts': [part]})
            previous_role = role
            continue
        if role not in {'user', 'assistant'}:
            raise ValueError('ai_model_role_invalid')
        parts = []
        for part in _parts(message.get('content')):
            if part['type'] == 'text':
                parts.append({'text': part['text']})
            else:
                prefix, body = part['image_url']['url'].split(',', 1)
                try:
                    data = base64.b64decode(body, validate=True)
                except ValueError:
                    raise ValueError('ai_model_image_invalid') from None
                parts.append({'inline_data': {'mime_type': prefix[5:].split(';')[0], 'data': data}})
        if role == 'assistant' and parts and isinstance(message.get('content'), list):
            for original in message['content']:
                if (isinstance(original, dict) and original.get('type') == 'think'
                        and isinstance(original.get('encrypted'), str) and original['encrypted']):
                    try:
                        parts[0]['thought_signature'] = base64.b64decode(original['encrypted'], validate=True)
                    except ValueError:
                        pass
                    break
        for call in _calls(message, signatures=True):
            function = call['function']
            try:
                args = json.loads(function['arguments'])
                signature = (base64.b64decode(call['_gemini_signature'], validate=True)
                             if call.get('_gemini_signature') else b'skip_thought_signature_validator')
            except (ValueError, TypeError):
                raise ValueError('ai_model_tool_invalid') from None
            if not isinstance(args, dict) or call['id'] in call_names:
                raise ValueError('ai_model_tool_invalid')
            call_names[call['id']] = function['name']
            parts.append({'function_call': {'name': function['name'], 'args': args},
                          'thought_signature': signature})
        if parts:
            result.append({'role': 'model' if role == 'assistant' else 'user', 'parts': parts})
        previous_role = role
    return result
