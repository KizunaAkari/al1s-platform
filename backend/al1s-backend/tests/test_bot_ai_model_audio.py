import asyncio
import io
import wave
from types import SimpleNamespace
from uuid import uuid4

import pytest

from al1s.bot_ai.model_audio import synthesize, transcribe


def test_transcription_returns_actual_model_text():
    async def run():
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text='听到的内容')
        client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
        assert await transcribe(client, 'whisper-model', b'wav') == '听到的内容'
        assert calls[0]['model'] == 'whisper-model' and calls[0]['file'][1] == b'wav'
    asyncio.run(run())


def test_openai_speech_is_written_to_own_request_directory(tmp_path):
    async def run():
        class Response:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def iter_bytes(self):
                yield b'RIFF-fixture'
        calls = []
        endpoint = SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or Response())
        client = SimpleNamespace(audio=SimpleNamespace(speech=SimpleNamespace(with_streaming_response=endpoint)))
        request = uuid4()
        (tmp_path / str(request)).mkdir()
        path = await synthesize(client, 'openai_tts_api', 'model', 'alloy', 'hello', tmp_path, request)
        assert path.read_bytes() == b'RIFF-fixture'
        assert calls[0]['response_format'] == 'wav' and calls[0]['voice'] == 'alloy'
    asyncio.run(run())


def test_gemini_speech_converts_pcm_to_wave_and_rejects_outside_directory(tmp_path):
    async def run():
        async def generate_content(**_kwargs):
            return {'candidates': [{'content': {'parts': [{'inline_data': {'data': b'\x00\x00' * 32}}]}}]}
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
        request = uuid4()
        (tmp_path / str(request)).mkdir()
        path = await synthesize(client, 'gemini_tts', 'model', 'Leda', 'hello', tmp_path, request)
        with wave.open(io.BytesIO(path.read_bytes())) as wav:
            assert wav.getframerate() == 24000 and wav.getnchannels() == 1 and wav.getnframes() == 32
        with pytest.raises(ValueError, match='directory'):
            await synthesize(client, 'gemini_tts', 'model', 'Leda', 'hello', tmp_path, uuid4())
    asyncio.run(run())
