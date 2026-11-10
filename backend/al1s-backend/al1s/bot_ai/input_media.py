import asyncio
import base64
import os
import stat
from pathlib import Path

from al1s.bot_ai.managed.smart_contract import display_name

_MAX_BYTES = 8 * 1024 * 1024


def _read(root, request_id, reference):
    directory = Path(root).resolve() / str(request_id)
    path = Path(reference)
    if (directory.is_symlink() or path.is_symlink() or path.parent.resolve() != directory.resolve()
            or directory.resolve().parent != Path(root).resolve()):
        raise ValueError('ai_media_path_invalid')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_BYTES:
            raise ValueError('ai_media_size_invalid')
        return file.read(_MAX_BYTES + 1)


async def build_user_message(envelope, media_root, *, transcribe=None, describe=None):
    parts = []
    if envelope.source.scope_kind == 'qq_group':
        name = display_name(envelope.sender_name, envelope.source.sender_id)
        label = f'[发言者 {name}]'
        if envelope.mentions:
            names = {item.sender_id: display_name(item.sender_name, item.sender_id)
                     for item in envelope.smart_context}
            targets = [f'{names.get(user, "群友")}（mention{index}）'
                       for index, user in enumerate(envelope.mentions, 1)]
            label += ' [本消息提及 ' + '、'.join(targets) + ']'
        parts.append({'type': 'text', 'text': label})
    for part in envelope.parts:
        if part.type == 'text':
            parts.append({'type': 'text', 'text': part.text})
        elif part.type == 'image' and part.observation_only:
            continue
        elif part.type == 'image':
            body = await asyncio.to_thread(_read, media_root, envelope.request_id, part.media_ref)
            image = 'data:image/png;base64,' + base64.b64encode(body).decode('ascii')
            if describe is not None:
                caption = await describe(image, part.caption_hint)
                if not isinstance(caption, str) or not caption.strip():
                    raise ValueError('ai_vision_unavailable')
                parts.append({'type': 'text', 'text': '[图片识别结果] ' + caption[:4000]})
            else:
                if part.caption_hint:
                    parts.append({'type': 'text', 'text': part.caption_hint})
                parts.append({'type': 'image_url', 'image_url': {'url': image}})
        elif part.type == 'record':
            if transcribe is None:
                raise ValueError('ai_transcription_unavailable')
            body = await asyncio.to_thread(_read, media_root, envelope.request_id, part.media_ref)
            text = await transcribe(body)
            if not isinstance(text, str) or not text.strip():
                raise ValueError('ai_transcription_unavailable')
            parts.append({'type': 'text', 'text': '[语音识别结果] ' + text[:12000]})
    if not parts:
        raise ValueError('ai_input_empty')
    return {'role': 'user', 'content': parts}
