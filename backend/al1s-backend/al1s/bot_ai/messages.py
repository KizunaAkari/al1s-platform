import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from al1s.bot_ai.managed.smart_contract import display_name
from al1s.bot_ai.settings import AiSettingsRecord
from al1s.meme_workshop.contracts import CommandSource
from al1s.meme_workshop.onebot import identity


@dataclass(frozen=True)
class AiIncoming:
    source: CommandSource
    self_id: str
    sender_name: str
    parts: tuple[dict, ...]
    mentions: tuple[str, ...]
    verify_reply_id: str | None = None
    smart: bool = False
    addressed: bool = False
    context: tuple[dict, ...] = ()
    observed_images: tuple[str, ...] = ()
    sticker_collect: bool = False
    collect_reply_id: str | None = None
    collect_reply_invalid: bool = False


def persona_command(text: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    match = re.fullmatch(r"切换人格(?:\s+(.+))?", normalized, re.DOTALL)
    return (match.group(1) or "").strip() if match else None


def is_sticker_collect(event: object) -> bool:
    if (not isinstance(event, dict) or event.get("post_type") != "message"
            or event.get("message_type") != "group" or not isinstance(event.get("message"), list)
            or len(event["message"]) > 128):
        return False
    own = identity(event.get("self_id"))
    text, mentioned = [], False
    for segment in event["message"]:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            return False
        data = segment["data"]
        if segment.get("type") == "at" and own and identity(data.get("qq")) == own:
            mentioned = True
        elif segment.get("type") == "text" and isinstance(data.get("text"), str):
            text.append(data["text"])
    return bool(mentioned and unicodedata.normalize("NFKC", "".join(text)).strip() == "收藏")


def parse_ai_event(event: object, settings: AiSettingsRecord) -> AiIncoming | None:
    if (not settings.enabled or settings.desired_version != settings.applied_version
            or not isinstance(event, dict) or event.get("post_type") != "message"):
        return None
    kind = event.get("message_type")
    sender, own = identity(event.get("user_id")), identity(event.get("self_id"))
    target = identity(event.get("group_id")) if kind == "group" else sender
    message_id, timestamp, segments = event.get("message_id"), event.get("time"), event.get("message")
    if (kind not in {"group", "private"} or not sender or not own or sender == own or not target
            or isinstance(message_id, bool) or not isinstance(message_id, (str, int))
            or not re.fullmatch(r"-?[0-9]{1,64}", str(message_id))
            or type(timestamp) is not int or not isinstance(segments, list) or len(segments) > 128):
        return None
    now = int(datetime.now(UTC).timestamp())
    if timestamp < now - 120 or timestamp > now + 30:
        return None
    if (kind == "group" and target not in settings.groups) or (
            kind == "private" and not settings.allow_private):
        return None
    parts, mentions, words = [], [], []
    at_self, reply_id, reply_count = False, None, 0
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            return None
        data, part_type = segment["data"], segment.get("type")
        if part_type == "text" and isinstance(data.get("text"), str):
            value = data["text"]
            if len(value) > 16384:
                return None
            if value.strip():
                words.append(value)
                parts.append({"type": "text", "text": value})
        elif part_type == "at":
            mention = identity(data.get("qq"))
            if mention == own:
                at_self = True
            elif mention and mention not in mentions:
                mentions.append(mention)
        elif part_type == "reply":
            reply_count += 1
            candidate = str(data.get("id", ""))
            if re.fullmatch(r"-?[0-9]{1,64}", candidate):
                reply_id = candidate
        elif part_type == "face":
            face_id = str(data.get("id", ""))
            if re.fullmatch(r"[0-9]{1,6}", face_id):
                parts.append({"type": "text", "text": f"[QQ表情#{face_id}]"})
        elif part_type in {"image", "record", "mface"}:
            if part_type == "record":
                size = data.get("file_size")
                if size is not None and (len(str(size)) > 20 or not str(size).isascii() or not str(size).isdigit()
                                         or int(size) > 8 * 1024 * 1024):
                    return None
                reference = data.get("file") or data.get("url")
            else:
                reference = data.get("url") or data.get("file")
            if part_type == "mface" and not reference:
                parts.append({"type": "text", "text": "[QQ表情包：当前消息未提供可读取的图片]"})
                continue
            if not isinstance(reference, str) or len(reference) > 2048:
                return None
            parts.append({"type": "image" if part_type == "mface" else part_type,
                          "reference": reference})
        elif part_type in {"video", "file"}:
            return None
    if (sum(p["type"] == "image" for p in parts) > 4
            or sum(p["type"] == "record" for p in parts) > 1 or len(mentions) > 4
            or not parts):
        return None
    text = unicodedata.normalize("NFKC", "".join(words)).strip().casefold()
    collect = is_sticker_collect(event)
    wake = any(text.startswith(word.casefold()) for word in settings.wake_words)
    command = persona_command(text) is not None
    reply_all = kind == "group" and target in settings.reply_all_groups
    smart = kind == "group" and target in settings.smart_reply_groups
    if (reply_all or smart) and not (at_self or wake or command):
        usable = [part for part in parts if not (
            (part["type"] == "image" and not settings.vision_enabled)
            or (part["type"] == "record" and not settings.stt_enabled))]
        if usable:
            parts = usable
        elif reply_id:
            reply_all = False
        else:
            return None
    if kind == "group" and not (at_self or wake or reply_id or command or reply_all or smart):
        return None
    sender_data = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    name = (display_name(sender_data.get("card"), sender, "")
            or display_name(sender_data.get("nickname"), sender, "")
            or ("群友" if kind == "group" else "用户"))
    return AiIncoming(CommandSource(message_id=str(message_id),
        scope_kind="qq_group" if kind == "group" else "qq_private", target_id=target,
        sender_id=sender), own, name, tuple(parts), tuple(mentions),
        reply_id if kind == "group" and not (at_self or wake or command or reply_all or smart) else None,
        smart=smart, addressed=bool(at_self or wake), sticker_collect=collect,
        collect_reply_id=reply_id if collect else None,
        collect_reply_invalid=bool(collect and (reply_count > 1 or (reply_count and not reply_id))))
