import time
from collections import OrderedDict, deque
from dataclasses import replace

from al1s.bot_ai.messages import AiIncoming


def merge_fragments(previous: AiIncoming, current: AiIncoming) -> AiIncoming:
    if (previous.source.sender_id != current.source.sender_id
            or any(part["type"] != "text" for part in previous.parts + current.parts)):
        return current
    text = "\n".join(part["text"] for part in previous.parts + current.parts)
    if len(text) > 16384:
        return current
    return replace(current, parts=({"type": "text", "text": text},))


class SmartReplyWindows:
    def __init__(self):
        self._groups = OrderedDict()
        self._images = {}

    def observe(self, key: tuple, incoming: AiIncoming, *, now=None) -> bool:
        now = time.monotonic() if now is None else now
        records, seen = self._groups.setdefault(key, (deque(maxlen=12), deque(maxlen=80)))
        self._groups.move_to_end(key)
        if len(self._groups) > 512:
            expired, _ = self._groups.popitem(last=False)
            self._images.pop(expired, None)
        if incoming.source.message_id in seen:
            return False
        seen.append(incoming.source.message_id)
        images = self._images.setdefault(key, deque(maxlen=48))
        images.extend((now, part["reference"]) for part in incoming.parts if part["type"] == "image")
        text = " ".join(part["text"] if part["type"] == "text" else
                        "[图片/表情包]" if part["type"] == "image" else "[语音]"
                        for part in incoming.parts)
        records.append((now, {"message_id": incoming.source.message_id,
            "sender_id": incoming.source.sender_id, "sender_name": incoming.sender_name,
            "text": text[:400]}))
        return True

    def snapshot(self, key: tuple, *, now=None) -> tuple[dict, ...]:
        now = time.monotonic() if now is None else now
        records, _ = self._groups.get(key, ((), ()))
        return tuple({**record, "age_seconds": max(0, now - timestamp)}
                     for timestamp, record in records if now - timestamp <= 120)

    def forget(self, key: tuple) -> None:
        self._groups.pop(key, None)
        self._images.pop(key, None)

    def take_images(self, key: tuple) -> tuple[str, ...]:
        now = time.monotonic()
        return tuple(ref for timestamp, ref in self._images.pop(key, ()) if now - timestamp <= 120)

    def forget_service(self, service_id) -> None:
        for key in tuple(self._groups):
            if key[0] == service_id:
                self.forget(key)

    def clear(self) -> None:
        self._groups.clear()
        self._images.clear()
