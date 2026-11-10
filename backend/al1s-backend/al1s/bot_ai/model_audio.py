import asyncio
import io
import os
import wave
from pathlib import Path

from al1s.bot_ai.model_provider import _dict

_MAX_BYTES = 8 * 1024 * 1024


async def transcribe(client, model, body):
    if not isinstance(body, bytes) or not 0 < len(body) <= _MAX_BYTES:
        raise ValueError('ai_audio_invalid')
    result = await client.audio.transcriptions.create(model=model, file=('audio.wav', body, 'audio/wav'))
    value = result.get('text') if isinstance(result, dict) else getattr(result, 'text', None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError('ai_transcription_unavailable')
    return value[:12000]


async def _openai_audio(client, model, voice, text):
    output = bytearray()
    async with client.audio.speech.with_streaming_response.create(
            model=model, voice=voice or 'alloy', response_format='wav', input=text) as response:
        async for chunk in response.iter_bytes():
            output.extend(chunk)
            if len(output) > _MAX_BYTES:
                raise ValueError('ai_audio_size_invalid')
    return bytes(output)


async def _gemini_audio(client, model, voice, text):
    response = _dict(await client.models.generate_content(model=model, contents=text, config={
        'response_modalities': ['AUDIO'],
        'speech_config': {'voice_config': {'prebuilt_voice_config': {'voice_name': voice or 'Leda'}}},
    }))
    candidates = response.get('candidates') or []
    content = _dict(_dict(candidates[0]).get('content') or {}) if candidates else {}
    for raw in content.get('parts') or []:
        item = _dict(raw)
        audio = _dict(item['inline_data']).get('data') if item.get('inline_data') else None
        if isinstance(audio, bytes) and 0 < len(audio) <= _MAX_BYTES - 44:
            buffer = io.BytesIO()
            with wave.open(buffer, 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24000)
                output.writeframes(audio)
            return buffer.getvalue()
    raise ValueError('ai_audio_unavailable')


def _write(root, request_id, body):
    root = Path(root).resolve()
    directory = root / str(request_id)
    if directory.is_symlink() or directory.resolve().parent != root or not directory.is_dir():
        raise ValueError('ai_audio_directory_invalid')
    if not body or len(body) > _MAX_BYTES:
        raise ValueError('ai_audio_size_invalid')
    target = directory / 'output-0-record.wav'
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o640)
    with os.fdopen(descriptor, 'wb') as file:
        file.write(body)
    return target


async def synthesize(client, kind, model, voice, text, root, request_id):
    if not isinstance(text, str) or not 0 < len(text) <= 500:
        raise ValueError('ai_audio_text_invalid')
    if kind == 'openai_tts_api':
        body = await _openai_audio(client, model, voice, text)
    elif kind == 'gemini_tts':
        body = await _gemini_audio(client, model, voice, text)
    else:
        raise ValueError('ai_audio_provider_invalid')
    task = asyncio.create_task(asyncio.to_thread(_write, root, request_id, body))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
