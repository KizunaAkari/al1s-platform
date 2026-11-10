import base64
import copy

import pytest

from al1s.bot_ai.model_messages import for_chat, for_gemini, for_responses


def sample():
    image = 'data:image/png;base64,' + base64.b64encode(b'fake-png').decode()
    return [
        {'role': 'user', 'content': [{'type': 'text', 'text': '问'},
                                     {'type': 'image_url', 'image_url': {'url': image}}]},
        {'role': 'assistant', 'content': [{'type': 'think', 'think': 'private', 'encrypted': 'x'},
                                         {'type': 'text', 'text': '查一下'}],
         'tool_calls': [{'id': 'call-1', 'type': 'function', 'function':
                         {'name': 'search_feeds', 'arguments': '{"query":"test"}'}}]},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': '{"status":"ok"}'},
        {'role': 'assistant', 'content': [{'type': 'think', 'think': 'private', 'encrypted': 'x'},
                                         {'type': 'text', 'text': '答'}]},
    ]


def test_chat_input_preserves_tool_pair_and_images_without_mutating_history():
    original = sample()
    before = copy.deepcopy(original)
    output = for_chat(original)
    assert original == before
    assert output[0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
    assert output[1]['content'] == [{'type': 'text', 'text': '查一下'}]
    assert output[1]['tool_calls'][0]['id'] == output[2]['tool_call_id'] == 'call-1'
    assert all('think' not in str(message) for message in output)


def test_responses_input_keeps_function_calls_and_results_in_order():
    output = for_responses(sample())
    assert output[0]['content'][1]['type'] == 'input_image'
    call = next(item for item in output if item.get('type') == 'function_call')
    result = next(item for item in output if item.get('type') == 'function_call_output')
    assert call['call_id'] == result['call_id'] == 'call-1'
    assert output.index(call) < output.index(result)


def test_responses_replays_original_encrypted_reasoning_before_tool_call():
    import json
    messages = sample()
    reasoning = {'type':'reasoning','id':'r1','summary':[],'encrypted_content':'opaque'}
    messages[1]['content'][0]['encrypted'] = json.dumps({'type':'openai_responses_reasoning','items':[reasoning]})
    output = for_responses(messages)
    assert reasoning in output
    assert output.index(reasoning) < next(i for i, item in enumerate(output) if item.get('type') == 'function_call')


def test_gemini_input_keeps_function_pair_and_legacy_signature_fallback():
    output = for_gemini(sample())
    assert output[0]['parts'][1]['inline_data']['data'] == b'fake-png'
    call = next(part for message in output for part in message['parts'] if 'function_call' in part)
    result = next(part for message in output for part in message['parts'] if 'function_response' in part)
    assert call['function_call']['name'] == result['function_response']['name'] == 'search_feeds'
    assert call['thought_signature'] == b'skip_thought_signature_validator'


def test_gemini_preserves_existing_astrbot_tool_signature():
    messages = sample()
    messages[1]['tool_calls'][0]['extra_content'] = {'google': {'thought_signature':
        base64.b64encode(b'existing-signature').decode()}}
    output = for_gemini(messages)
    call = next(part for message in output for part in message['parts'] if 'function_call' in part)
    assert call['thought_signature'] == b'existing-signature'


def test_gemini_preserves_existing_signature_on_assistant_text():
    messages = [{'role':'user','content':'hi'}, {'role':'assistant','content':[
        {'type':'think','think':'','encrypted':base64.b64encode(b'text-signature').decode()},
        {'type':'text','text':'hello'}]}]
    output = for_gemini(messages)
    assert output[1]['parts'][0]['thought_signature'] == b'text-signature'


def test_empty_legacy_thought_signature_is_not_forwarded():
    messages = [{'role':'user','content':'hi'}, {'role':'assistant','content':[
        {'type':'think','think':'','encrypted':''}, {'type':'text','text':'hello'}]}]
    assert 'thought_signature' not in for_gemini(messages)[1]['parts'][0]


def test_unknown_content_and_orphan_tool_result_are_rejected():
    with pytest.raises(ValueError, match='content'):
        for_chat([{'role': 'user', 'content': [{'type': 'unexpected'}]}])
    with pytest.raises(ValueError, match='tool'):
        for_gemini([{'role': 'tool', 'tool_call_id': 'missing', 'content': '{}'}])
