import asyncio
import re
from types import SimpleNamespace

from al1s.bot_ai.agent import run_agent
from al1s.bot_ai.gpu_queue import gpu_owner
from al1s.bot_ai.input_media import build_user_message
from al1s.bot_ai.managed.presentation import compose_reply
from al1s.bot_ai.managed.smart_reply import SmartReplyPolicy


def _forbids_sticker(text):
    return bool(re.search(r'(?:不要|别|不用|不必|不想|停止|禁止).{0,8}(?:表情包|表情|动图)',
                          re.sub(r'\s+', '', text)))


class _StickerTurn:
    def __init__(self, library, envelope):
        self.library, self.envelope = library, envelope
        self.candidates, self.identity, self.path = set(), None, None

    async def search(self, query):
        item = self.envelope
        result = await self.library.search(item.service_id, item.source.target_id, query)
        self.candidates = {row['id'] for row in result}
        return result

    async def stage(self, identity):
        item = self.envelope
        self.path = str(await self.library.stage(item.service_id, item.source.target_id,
                                                 identity, item.request_id, self.candidates))
        self.identity = identity
        return self.path

    def allowed(self):
        item = self.envelope
        return bool(self.identity and self.library.allowed_now(item.service_id, item.source.target_id,
                                                               self.identity))


class AiExecutor:
    def __init__(self, profiles, history, media_root, *, policy=None, stickers=None,
                 stt=None, tts=None, vision=None, text_slots=None):
        self.profiles, self.history, self.media_root = profiles, history, media_root
        self.policy = policy or SmartReplyPolicy()
        self.stickers, self.stt, self.tts, self.vision = stickers, stt, tts, vision
        self.text_slots = text_slots or asyncio.Semaphore(2)

    async def clear_session(self, service_id, scope, target):
        task = asyncio.create_task(asyncio.to_thread(self.history.clear, service_id, scope, target))
        cancelled = False
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            await task
        self.policy.forget_origin(f'{service_id}:{scope}:{target}')
        if cancelled:
            raise asyncio.CancelledError
        return {'state': 'cleared'}

    async def sticker_control(self, service_id, command):
        if not self.stickers:
            raise ValueError('ai_sticker_unavailable')
        return await self.stickers.control(service_id, command)

    async def _decision(self, item, profile, current, sticker_turn):
        text = '\n'.join(part.get('text', '') for part in current['content'] if part.get('type') == 'text')
        options, candidates = item.smart_reply, []
        available = {'text'}
        if options.voice_enabled and item.tts_enabled and self.tts:
            available.add('voice')
        if sticker_turn and options.stickers_enabled and not _forbids_sticker(text):
            candidates = await sticker_turn.search(text)
            if candidates:
                available.add('sticker')

        async def ask(*, prompt, system_prompt, **_kwargs):
            response = await profile.provider.generate(system_prompt,
                [{'role': 'user', 'content': prompt}], (), json_output=True)
            return SimpleNamespace(completion_text=response['text'])

        key = f'{item.service_id}:{item.source.scope_kind}:{item.source.target_id}'
        decision = await self.policy.decide(key, item.source.sender_id, text, options, ask, available,
            addressed=item.addressed, persona=item.persona_prompt, history=item.smart_context,
            message_id=item.source.message_id, cache_identity=str(item.chat_profile_id),
            sender_name=item.sender_name, sticker_candidates=candidates)
        return key, decision, candidates

    async def run(self, item, invocation):
        profile = self.profiles.get(item.chat_profile_id)
        if profile is None:
            raise ValueError('ai_profile_unconfigured')
        separate = bool(item.vision_provider_id and item.vision_provider_id.endswith('_vision'))
        has_image = any(part.type == 'image' and not part.observation_only for part in item.parts)
        if has_image and ((separate and self.vision is None) or (not separate and not profile.supports_vision)):
            raise ValueError('ai_vision_unavailable')
        context = await invocation.blocking(self.history.context, item.service_id,
                                             item.source.scope_kind, item.source.target_id)
        token = gpu_owner.set((item.service_id, item.source.sender_id))
        try:
            current = await build_user_message(item, self.media_root, transcribe=self.stt,
                                               describe=self.vision if separate else None)
        finally:
            gpu_owner.reset(token)
        async with self.text_slots:
            async with asyncio.timeout(120):
                return await self._respond(item, invocation, profile, context, current)

    async def _respond(self, item, invocation, profile, context, current):
        text_input = '\n'.join(part.get('text', '') for part in current['content'] if part.get('type') == 'text')
        sticker_turn = (_StickerTurn(self.stickers, item) if self.stickers and item.sticker_enabled
                        and item.source.scope_kind == 'qq_group' and not _forbids_sticker(text_input) else None)
        key, decision, candidates = None, None, []
        if item.smart_reply is not None:
            key, decision, candidates = await self._decision(item, profile, current, sticker_turn)
            if not decision.should_reply:
                return 'completed', decision.code
        system = item.persona_prompt + ('\n图片或语音识别失败时，请明确说明无法识别，不要猜测内容。'
            '\n只在用户确实询问时使用业务工具；表情候选只能来自本群。')
        channels = tuple(decision.channels) if decision else (('text', 'voice') if item.tts_enabled and self.tts else ('text',))
        allow_stickers = sticker_turn if sticker_turn and (decision is None or 'sticker' in channels) else None
        text, turn, attachments = await run_agent(profile.provider, system, context.messages, current,
                                                   invocation, item.mentions, stickers=allow_stickers)
        await invocation.blocking(self.history.append, context, item.request_id, turn,
                                  epoch=invocation.epoch, config_version=item.config_version)
        parts = await self._compose(item, text, channels, attachments, sticker_turn, decision, candidates)
        if sticker_turn and sticker_turn.path and any(part.get('media_ref') == sticker_turn.path for part in parts):
            invocation.guard_image(sticker_turn.path, sticker_turn.allowed, text)
        if not parts:
            return 'completed', None
        result = await invocation.output(0, parts)
        status = result.get('status') if isinstance(result, dict) else None
        if (isinstance(result, dict) and sticker_turn and sticker_turn.path
                and sticker_turn.path in result.get('sent_media', [])):
            try:
                await self.stickers.record_sent(item.service_id, item.source.target_id, sticker_turn.identity)
            except Exception:
                pass
        if status == 'sent' and decision is not None:
            self.policy.sent(key, item.smart_reply, text=text)
        return ('completed' if status == 'sent' else 'unknown' if status == 'unknown' else 'failed'), None

    async def _compose(self, item, text, channels, attachments, sticker_turn, decision, candidates):
        charts = [path for path in attachments if not sticker_turn or path != sticker_turn.path]
        audio_failed = False

        async def voice(value):
            nonlocal audio_failed
            try:
                return await self.tts(value, item.request_id) if self.tts else None
            except Exception:
                audio_failed = True
                return None

        async def sticker(value):
            if not sticker_turn or charts:
                return None
            if sticker_turn.path:
                return sticker_turn.path
            index = getattr(decision, 'sticker_index', None) if decision else None
            if index and 1 <= index <= len(candidates):
                return await sticker_turn.stage(candidates[index - 1]['id'])
            choices = await sticker_turn.search('\n'.join(part.text or '' for part in item.parts
                                                            if part.type == 'text') + '\n' + value)
            return await sticker_turn.stage(choices[0]['id']) if choices else None

        if sticker_turn and sticker_turn.path and not charts and 'sticker' not in channels:
            channels = (*channels, 'sticker')
        parts = await compose_reply(text, channels, tts=voice, sticker=sticker)
        if charts:
            parts.append({'type': 'image', 'media_ref': charts[0]})
        if audio_failed and text:
            note = '\n（语音不可用，已保留文字回复。）'
            target = next((part for part in parts if part['type'] == 'text'), None)
            if target:
                target['text'] += note
        elif item.tts_enabled and len(text) > 500 and text:
            target = next((part for part in parts if part['type'] == 'text'), None)
            if target:
                target['text'] += '\n（回复较长，本次仅发送文字。）'
        for part in parts:
            if part['type'] == 'record' and text:
                part['fallback_text'] = text[:3000]
        return parts
