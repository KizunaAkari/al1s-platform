"""Run the existing, bounded QQ read-only tool set for one model turn."""

import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from al1s.bot_ai.tools import ChartArgs, FeedArgs, MarketArgs, MemeArgs, QueryArgs

_TOOLS = {
    'query_trading': (QueryArgs, '查询发言者自己的A股或虚拟币模拟账户、持仓或成交。'),
    'crypto_chart': (ChartArgs, '生成指定虚拟币的历史走势图。'),
    'market_quote': (MarketArgs, '查询公开行情，结果包含来源与报价时间。'),
    'search_feeds': (FeedArgs, '按关键词查询近期公开财经快讯。'),
    'send_meme': (MemeArgs, '按明确要求发送互动表情；target可为self或本轮mention1等。'),
}
TOOL_DEFINITIONS = tuple({'name': name, 'description': description,
                          'parameters': model.model_json_schema()}
                         for name, (model, description) in _TOOLS.items())


class _StickerSearch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=500)


class _StickerStage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sticker_id: str = Field(min_length=1, max_length=64)


_STICKER_TOOLS = (
    {'name': 'search_sticker', 'description': '搜索本群已收藏的表情，返回本轮候选。',
     'parameters': _StickerSearch.model_json_schema()},
    {'name': 'send_sticker', 'description': '从本轮候选中选择表情，加入本轮回复。',
     'parameters': _StickerStage.model_json_schema()},
)


async def _sticker_tool(stickers, name, raw, choices):
    try:
        arguments = json.loads(raw)
        if name == 'search_sticker':
            query = _StickerSearch.model_validate(arguments).query
            found = await stickers.search(query)
            choices.clear()
            for item in found[:5]:
                identity = item.get('id')
                if isinstance(identity, str) and re.fullmatch(r'[a-f0-9]{64}', identity):
                    choices[str(len(choices) + 1)] = identity
            return {'status': 'success', 'items': found[:5]}, None
        requested = _StickerStage.model_validate(arguments).sticker_id
        identity = choices.get(requested, requested)
        if identity not in choices.values():
            return {'status': 'denied', 'code': 'ai_sticker_candidate_required'}, None
        path = await stickers.stage(identity)
        return {'status': 'staged'}, str(path)
    except (ValueError, TypeError, ValidationError, OSError):
        return {'status': 'failed', 'code': 'ai_sticker_unavailable'}, None


async def _execute(invocation, call, mentions):
    function = call.get('function') or {}
    name, raw = function.get('name'), function.get('arguments')
    entry = _TOOLS.get(name)
    if entry is None or not isinstance(raw, str) or len(raw) > 16384:
        return {'status': 'denied', 'code': 'ai_tool_not_allowed'}
    try:
        arguments = json.loads(raw)
        if not isinstance(arguments, dict):
            raise ValueError('invalid arguments')
        arguments = entry[0].model_validate(arguments).model_dump(exclude_none=True)
        if name == 'send_meme':
            target = arguments.get('target', 'self')
            match = re.fullmatch(r'mention([1-4])', target)
            if match:
                index = int(match.group(1)) - 1
                if index >= len(mentions):
                    return {'status': 'denied', 'code': 'ai_meme_target_denied'}
                arguments['target'] = mentions[index]
            elif target != 'self' and target not in mentions:
                return {'status': 'denied', 'code': 'ai_meme_target_denied'}
    except (ValueError, TypeError, ValidationError):
        return {'status': 'denied', 'code': 'ai_tool_arguments_invalid'}
    return await invocation.tool(call['id'], name, arguments)


async def run_agent(provider, system, history, current, invocation, mentions, *, stickers=None):
    messages, turn, attachments = [*history, current], [current], []
    choices = {}
    calls_used = 0
    for _ in range(5):
        definitions = (*TOOL_DEFINITIONS, *_STICKER_TOOLS) if stickers else TOOL_DEFINITIONS
        reply = await provider.generate(system, messages, definitions)
        text, calls = reply['text'], reply['tool_calls']
        if not isinstance(text, str) or not isinstance(calls, list):
            raise ValueError('ai_model_response_invalid')
        calls_used += len(calls)
        if calls_used > 4:
            raise ValueError('ai_tool_limit')
        assistant = {'role': 'assistant', 'content': text or None}
        reasoning = reply.get('reasoning_items') or []
        if reasoning:
            if not isinstance(reasoning, list) or any(not isinstance(item, dict) for item in reasoning):
                raise ValueError('ai_model_response_invalid')
            state = json.dumps({'type':'openai_responses_reasoning', 'items':reasoning}, ensure_ascii=False)
            if len(state) > 2 * 1024 * 1024:
                raise ValueError('ai_model_response_invalid')
            assistant['content'] = [{'type':'think', 'think':'', 'encrypted':state}]
            if text:
                assistant['content'].append({'type':'text', 'text':text})
        elif reply.get('thinking_signature'):
            signature = reply['thinking_signature']
            if not isinstance(signature, str) or len(signature) > 2 * 1024 * 1024:
                raise ValueError('ai_model_response_invalid')
            assistant['content'] = [{'type':'think', 'think':'', 'encrypted':signature}]
            if text:
                assistant['content'].append({'type':'text', 'text':text})
        if calls:
            assistant['tool_calls'] = calls
        turn.append(assistant)
        messages.append(assistant)
        if not calls:
            return text, turn, attachments
        for call in calls:
            function = call.get('function') or {}
            name, raw = function.get('name'), function.get('arguments')
            if stickers and name in {'search_sticker', 'send_sticker'} and isinstance(raw, str) and len(raw) <= 16384:
                result, path = await _sticker_tool(stickers, name, raw, choices)
                if path:
                    attachments.append(path)
            else:
                result = await _execute(invocation, call, mentions)
            if not isinstance(result, dict):
                result = {'status': 'failed', 'code': 'ai_tool_result_invalid'}
            if result.get('status') == 'success' and isinstance(result.get('media_ref'), str):
                attachments.append(result['media_ref'])
                result = {key: value for key, value in result.items() if key != 'media_ref'}
                result['image_attached'] = True
            result_message = {'role': 'tool', 'tool_call_id': call['id'], 'name': name,
                              'content': json.dumps(result, ensure_ascii=False, separators=(',', ':'))}
            turn.append(result_message)
            messages.append(result_message)
    raise ValueError('ai_tool_limit')
