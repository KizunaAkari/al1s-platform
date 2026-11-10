import asyncio
import re
import time
import unicodedata
from io import BytesIO

from PIL import Image

from al1s.bot_drawing.contracts import DrawingCommand
from al1s.meme_workshop.contracts import CommandSource
from al1s.meme_workshop.images import MAX_BYTES, MAX_PIXELS
from al1s.meme_workshop.media_fetch import read_media
from al1s.meme_workshop.onebot import identity

_MESSAGE_ID = re.compile(r"-?[0-9]{1,64}")


class DrawingParseError(ValueError):
    def __init__(self, code: str, command: DrawingCommand):
        super().__init__(code)
        self.command = command


def parse_drawing_event(event: object, self_id: str) -> DrawingCommand | None:
    if not isinstance(event, dict) or event.get("post_type") != "message":
        return None
    kind = event.get("message_type")
    own, sender = identity(self_id), identity(event.get("user_id"))
    target = identity(event.get("group_id")) if kind == "group" else sender
    message_id, timestamp, segments = event.get("message_id"), event.get("time"), event.get("message")
    now = int(time.time())
    if (kind not in {"group", "private"} or not own or identity(event.get("self_id")) != own
            or not sender or sender == own or not target or isinstance(message_id, bool)
            or not isinstance(message_id, (str, int)) or not _MESSAGE_ID.fullmatch(str(message_id))
            or type(timestamp) is not int or timestamp < now - 300 or timestamp > now + 60
            or not isinstance(segments, list) or len(segments) > 128):
        return None

    words, images, replies = [], [], []
    mentioned = False
    unsupported = False
    for part in segments:
        if not isinstance(part, dict) or not isinstance(part.get("data"), dict):
            return None
        part_type, data = part.get("type"), part["data"]
        if part_type == "text":
            value = data.get("text")
            if not isinstance(value, str) or len(value) > 16384:
                return None
            words.append(value)
        elif part_type == "at":
            mentioned = mentioned or identity(data.get("qq")) == own
        elif part_type == "image":
            ref = data.get("url") or data.get("file")
            if not isinstance(ref, str) or not ref or len(ref) > 2048:
                raise ValueError("drawing_image_source_invalid")
            images.append(ref)
        elif part_type == "reply":
            ref = data.get("id")
            if isinstance(ref, bool) or not isinstance(ref, (str, int)) or not _MESSAGE_ID.fullmatch(str(ref)):
                raise ValueError("drawing_image_source_invalid")
            replies.append(str(ref))
        else:
            unsupported = True

    body = "".join(words).strip()
    normalized = unicodedata.normalize("NFKC", body)
    words = body.split(maxsplit=1)
    workflow = (re.fullmatch(r"/wf([0-9]{0,9})", unicodedata.normalize("NFKC", words[0]), re.IGNORECASE)
                if words else None)
    online = (re.fullmatch(r"/gpt([0-9]{0,9})", unicodedata.normalize("NFKC", words[0]), re.IGNORECASE)
              if words else None)
    tag = re.fullmatch(r"/tag", normalized, re.IGNORECASE)
    p2p = re.fullmatch(r"/p2p(?:\s+(.*))?", body, re.IGNORECASE | re.DOTALL)
    if tag and kind == "group" and not mentioned:
        return None
    if not (workflow or online or tag or p2p):
        return None
    source = CommandSource(message_id=str(message_id), scope_kind="qq_group" if kind == "group" else "qq_private",
                           target_id=target, sender_id=sender)
    if workflow:
        operation = "workflow_draw" if len(words) > 1 else "workflow"
        prompt = words[1].strip() if len(words) > 1 else body
    elif online:
        operation, prompt = "online_draw", words[1].strip() if len(words) > 1 else ""
    elif tag:
        operation, prompt = "tag", body
    else:
        operation, prompt = "p2p", (p2p.group(1) or "").strip()
    matched = workflow or online
    number = int(matched.group(1)) if matched and matched.group(1) else None
    command = DrawingCommand(source, own, operation, prompt, number, images[0] if images else None,
                             replies[0] if replies else None, timestamp, mentioned, False)

    def invalid(code):
        raise DrawingParseError(code, command)

    if len(images) > 1 or len(replies) > 1 or (images and replies):
        invalid("drawing_image_source_invalid")
    if unsupported:
        return None
    if len(prompt) > 2000:
        invalid("drawing_command_invalid")
    if operation == "workflow_draw" and not number:
        invalid("drawing_command_invalid")
    if operation == "online_draw" and number == 0:
        invalid("drawing_command_invalid")
    if (tag or p2p) and images:
        invalid("drawing_image_source_invalid")
    if operation == "workflow" and (images or replies):
        invalid("drawing_command_invalid")
    if not body:
        return None
    return command


async def resolve_reference(command: DrawingCommand, client) -> bytes | None:
    reference = command.image_reference
    if command.reference_message_id is not None:
        if reference is not None:
            raise ValueError("drawing_image_source_invalid")
        message = await asyncio.wait_for(asyncio.to_thread(client.post, "get_msg", {
            "message_id": int(command.reference_message_id),
        }), timeout=6)
        if not isinstance(message, dict):
            raise ValueError("drawing_image_scope_invalid")
        if command.source.scope_kind == "qq_group":
            same_scope = (message.get("message_type") == "group"
                          and identity(message.get("group_id")) == command.source.target_id)
        else:
            same_scope = (message.get("message_type") == "private"
                          and command.source.target_id in {identity(message.get("user_id")),
                              identity(message.get("target_id")), identity(message.get("peer_id"))})
        if not same_scope:
            raise ValueError("drawing_image_scope_invalid")
        if command.requires_reply_verification:
            sender = message.get("sender")
            if not isinstance(sender, dict) or identity(sender.get("user_id")) != command.self_id:
                raise ValueError("drawing_image_sender_invalid")
        segments = message.get("message")
        if not isinstance(segments, list) or len(segments) > 128:
            raise ValueError("drawing_image_source_invalid")
        images = [part for part in segments if isinstance(part, dict)
                  and part.get("type") in {"image", "mface"}]
        if not images and command.operation in {"workflow_draw", "online_draw"}:
            return None
        if len(images) != 1 or not isinstance(images[0].get("data"), dict):
            raise ValueError("drawing_image_source_invalid")
        data = images[0]["data"]
        reference = data.get("url") or data.get("file")
        if not isinstance(reference, str) or not reference or len(reference) > 2048:
            raise ValueError("drawing_image_source_invalid")
    if reference is None:
        return None

    body = await asyncio.wait_for(asyncio.to_thread(read_media, reference), timeout=6)
    if not isinstance(body, bytes) or not body or len(body) > MAX_BYTES:
        raise ValueError("drawing_image_invalid")
    try:
        with Image.open(BytesIO(body), formats=["PNG", "JPEG", "WEBP"]) as image:
            width, height = image.size
            if (getattr(image, "n_frames", 1) != 1 or min(width, height) < 1
                    or max(width, height) > 4096 or width * height > MAX_PIXELS):
                raise ValueError("drawing_image_invalid")
            image.load()
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError("drawing_image_invalid") from error
    return body
