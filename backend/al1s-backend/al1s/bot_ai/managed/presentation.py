import asyncio


async def compose_reply(text, channels, *, tts, sticker):
    parts = []
    voice = image = None
    if "voice" in channels and 0 < len(text) <= 500:
        try:
            voice = await asyncio.wait_for(tts(text), 20)
        except Exception:
            pass
    if "sticker" in channels:
        try:
            image = await asyncio.wait_for(sticker(text), 8)
        except Exception:
            pass
    if "text" in channels or ("voice" in channels and not voice) or (not voice and not image):
        if text:
            parts.append({"type": "text", "text": text})
    if voice:
        parts.append({"type": "record", "media_ref": voice})
    if image:
        parts.append({"type": "image", "media_ref": image})
    return parts
