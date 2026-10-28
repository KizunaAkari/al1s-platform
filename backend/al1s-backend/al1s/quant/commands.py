import re
import unicodedata
from collections.abc import Mapping, Set
from dataclasses import dataclass

from al1s.meme_workshop.contracts import CommandSource
from al1s.meme_workshop.onebot import identity
from al1s.quant.contracts import QuantSettings

DEFAULT_COMMANDS = {
    "open": "模拟开户",
    "buy": "模拟买入",
    "sell": "模拟卖出",
    "balance": "模拟资产",
    "positions": "模拟持仓",
    "orders": "模拟成交",
    "backtest": "回测",
    "help": "量化帮助",
}


@dataclass(frozen=True, slots=True)
class QuantCommand:
    source: CommandSource
    action: str
    arguments: tuple[str, ...]
    mapping_version: int
    event_timestamp: int
    normalized_keyword: str = ""
    settings_version: int = 0
    settings: QuantSettings | None = None
    command_words: tuple[tuple[str, str], ...] = ()


def normalize_keyword(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("quant_keyword_invalid")
    result = unicodedata.normalize("NFKC", value).casefold()
    if (
        not result
        or len(result) > 32
        or any(c.isspace() or unicodedata.category(c) in {"Cc", "Cs"} for c in result)
    ):
        raise ValueError("quant_keyword_invalid")
    return result


def validate_mappings(mapping: Mapping[str, str], meme_words: Set[str]) -> dict[str, str]:
    if set(mapping) != set(DEFAULT_COMMANDS):
        raise ValueError("quant_mapping_invalid")
    normalized = {action: normalize_keyword(word) for action, word in mapping.items()}
    if len(set(normalized.values())) != len(normalized) or set(normalized.values()) & {
        normalize_keyword(word) for word in meme_words
    }:
        raise ValueError("quant_keyword_conflict")
    return normalized


def parse_quant_event(
    event: object, mapping: Mapping[str, str], meme_words: Set[str], version: int
) -> QuantCommand | None:
    if not isinstance(event, dict) or event.get("post_type") != "message":
        return None
    kind = event.get("message_type")
    sender, own = identity(event.get("user_id")), identity(event.get("self_id"))
    target = identity(event.get("group_id")) if kind == "group" else sender
    message_id, timestamp = event.get("message_id"), event.get("time")
    segments = event.get("message")
    if (
        kind not in {"group", "private"}
        or not sender
        or not own
        or sender == own
        or not target
        or isinstance(message_id, bool)
        or not isinstance(message_id, (int, str))
        or not re.fullmatch(r"-?[0-9]{1,64}", str(message_id))
        or type(timestamp) is not int
        or timestamp <= 0
        or not isinstance(segments, list)
        or len(segments) > 128
        or version < 1
    ):
        return None
    words: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            return None
        data = segment["data"]
        if segment.get("type") == "at":
            if "".join(words).strip() or identity(data.get("qq")) != own:
                return None
        elif segment.get("type") == "text" and isinstance(data.get("text"), str):
            if len(data["text"]) > 16384:
                return None
            words.append(data["text"])
        elif segment.get("type") != "reply":
            return None
    parts = "".join(words).strip().split()
    if not parts:
        return None
    try:
        keyword = normalize_keyword(parts[0])
        normalized = {action: normalize_keyword(word) for action, word in mapping.items()}
    except ValueError:
        return None
    if keyword in {normalize_keyword(word) for word in meme_words}:
        return None
    matching = [action for action, word in normalized.items() if word == keyword]
    if len(matching) != 1 or matching[0] not in DEFAULT_COMMANDS:
        return None
    return QuantCommand(
        CommandSource(
            message_id=str(message_id),
            scope_kind="qq_group" if kind == "group" else "qq_private",
            target_id=target,
            sender_id=sender,
        ),
        matching[0],
        tuple(parts[1:]),
        version,
        timestamp,
        keyword,
    )
