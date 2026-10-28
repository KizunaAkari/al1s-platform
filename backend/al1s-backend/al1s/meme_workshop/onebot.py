import base64
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from al1s.infrastructure.direct_http import DirectHttpError, post_json_direct
from al1s.meme_workshop.contracts import CommandSource, RenderedImage


def identity(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    result = str(value)
    return result if re.fullmatch(r"[1-9][0-9]{0,19}", result) else None


@dataclass(frozen=True)
class QqMessage:
    source: CommandSource
    keyword: str
    avatar_ids: list[str]
    image_urls: list[str]
    texts: list[str]
    reply_id: str | None
    copied_target_text: str | None = None


def image_urls(message: object) -> list[str]:
    if not isinstance(message, list):
        return []
    return [
        segment["data"]["url"]
        for segment in message
        if isinstance(segment, dict)
        and segment.get("type") == "image"
        and isinstance(segment.get("data"), dict)
        and isinstance(segment["data"].get("url"), str)
    ]


def command_parts(body: str, keywords: set[str]) -> list[str]:
    parts = body.strip().split(maxsplit=1)
    if not parts or unicodedata.normalize("NFKC", parts[0]).casefold() in keywords:
        return parts
    markers = [index for index, char in enumerate(parts[0]) if char in {"@", "\uff20"}]
    if markers:
        offset = markers[0]
        prefix = parts[0][:offset]
        if unicodedata.normalize("NFKC", prefix).casefold() in keywords:
            tail = parts[0][offset:] + (" " + parts[1] if len(parts) > 1 else "")
            return [prefix, tail]
    return parts


def parse_event(event: object, keywords: set[str]) -> QqMessage | None:
    if not isinstance(event, dict) or event.get("post_type") != "message":
        return None
    kind = event.get("message_type")
    sender, self_id = identity(event.get("user_id")), identity(event.get("self_id"))
    segments = event.get("message")
    target = identity(event.get("group_id")) if kind == "group" else sender
    message_id = event.get("message_id")
    if (
        kind not in {"private", "group"}
        or not sender
        or not target
        or sender == self_id
        or not isinstance(segments, list)
        or len(segments) > 128
        or isinstance(message_id, bool)
        or not isinstance(message_id, (str, int))
        or not re.fullmatch(r"-?[0-9]{1,64}", str(message_id))
    ):
        return None
    words: list[str] = []
    mentions: list[str] = []
    reply: str | None = None
    started = False
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            continue
        data = segment["data"]
        if segment.get("type") == "at":
            user = identity(data.get("qq"))
            if not started and user != self_id:
                return None
            if started and user:
                mentions.append(user)
            words.append(" ")
        elif segment.get("type") == "text" and isinstance(data.get("text"), str):
            text = data["text"]
            if len(text) > 16384:
                return None
            words.append(text)
            started = started or bool(text.strip())
        elif segment.get("type") == "reply":
            value = str(data.get("id", ""))
            if re.fullmatch(r"-?[0-9]{1,64}", value):
                reply = value
    parts = command_parts("".join(words), keywords | {"头像合成", "随机表情", "表情列表"})
    if not parts:
        return None
    keyword = unicodedata.normalize("NFKC", parts[0]).casefold()
    tail = parts[1].strip() if len(parts) > 1 else ""
    if keyword == "头像合成":
        names = command_parts(tail, keywords)
        if not names:
            return None
        keyword = unicodedata.normalize("NFKC", names[0]).casefold()
        tail = names[1] if len(names) > 1 else ""
    elif keyword not in keywords | {"随机表情", "表情列表"}:
        return None
    texts = [part.strip() for part in tail.split("|") if part.strip()]
    copied = (
        texts[0]
        if not mentions and texts and unicodedata.normalize("NFKC", texts[0]).startswith("@")
        else None
    )
    return QqMessage(
        CommandSource.model_validate(
            {
                "message_id": str(message_id),
                "scope_kind": "qq_group" if kind == "group" else "qq_private",
                "target_id": target,
                "sender_id": sender,
            }
        ),
        keyword,
        mentions or ([] if copied else [sender]),
        image_urls(segments),
        texts,
        reply,
        copied,
    )


class OneBotClient:
    def __init__(self, base_url: str, token: str | None) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid_onebot_endpoint")
        self._base, self._token = base_url.rstrip("/"), token

    def _data(
        self, action: str, payload: dict[str, object], *, max_response_bytes: int = 65_536
    ) -> object:
        try:
            result = post_json_direct(
                self._base + "/" + action, self._token, payload, timeout_seconds=5,
                max_response_bytes=max_response_bytes,
            )
        except DirectHttpError as exc:
            raise ValueError("onebot_operation_unknown") from exc
        if (
            not isinstance(result, dict)
            or isinstance(result.get("retcode"), bool)
            or result.get("retcode") != 0
            or result.get("status") != "ok"
        ):
            raise ValueError("onebot_operation_rejected")
        return result.get("data")

    def post(self, action: str, payload: dict[str, object]) -> dict[str, Any]:
        data = self._data(action, payload)
        return data if isinstance(data, dict) else {}

    def group_members(self, group_id: str) -> list[dict[str, Any]]:
        from al1s.meme_workshop.copied_mentions import MAX_MEMBERS

        data = self._data(
            "get_group_member_list", {"group_id": int(group_id), "no_cache": True},
            max_response_bytes=8 * 1024 * 1024,
        )
        if not isinstance(data, list) or len(data) > MAX_MEMBERS:
            raise ValueError("onebot_members_invalid")
        if any(not isinstance(row, dict) for row in data):
            raise ValueError("onebot_members_invalid")
        if any(identity(row.get("group_id")) != group_id for row in data):
            raise ValueError("onebot_members_invalid")
        return data

    def reply_images(self, message_id: str) -> list[str]:
        return image_urls(self.post("get_msg", {"message_id": int(message_id)}).get("message"))

    def send(self, source: CommandSource, result: RenderedImage | str) -> None:
        segments: list[dict[str, object]] = [{"type": "reply", "data": {"id": source.message_id}}]
        if isinstance(result, str):
            segments.append({"type": "text", "data": {"text": result}})
        else:
            segments.append(
                {
                    "type": "image",
                    "data": {
                        "file": "base64://" + base64.b64encode(result.body).decode("ascii"),
                    },
                }
            )
        group = source.scope_kind == "qq_group"
        data = self.post(
            "send_group_msg" if group else "send_private_msg",
            {
                "group_id" if group else "user_id": int(source.target_id),
                "message": segments,
            },
        )
        if isinstance(data.get("message_id"), bool) or not isinstance(
            data.get("message_id"), (int, str)
        ):
            raise ValueError("onebot_receipt_unknown")
