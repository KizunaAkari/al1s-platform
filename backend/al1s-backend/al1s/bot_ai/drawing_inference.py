import json
import re
import unicodedata

from al1s.bot_drawing.contracts import PromptIntent
from al1s.bot_drawing.routing import normalize_lora_key

_LORA = re.compile(r"<lora\s*:\s*([^:>]{1,128})(?::[^>]{0,64})?>", re.IGNORECASE)
_LORA_WORD = re.compile(r"(?:使用|启用|加载)?\s*(?<![A-Za-z0-9_])lora(?![A-Za-z0-9_])\s*[:：=]?\s*([^\s,，;；<>]{1,128})", re.IGNORECASE)
_NEGATIVE = re.compile(r"(?:不要|排除)\s*([^，,；;\n]+)")
_NEGATIVE_MARKER = re.compile(r"【\s*(?:负面|负向|np)\s*】\s*[:：]?\s*", re.IGNORECASE)
_CHINESE_FIELDS = re.compile(r"^\s*正面\s*[:：]\s*(.*?)(?:[\s,，;；]+负面\s*[:：]\s*(.*))?$", re.DOTALL)
_FIELDS = re.compile(r"^\s*(?:正面|prompt|positive)\s*[:：]\s*(.*?)"
                     r"(?:[\s,，;；]+(?:负面|negative(?:\s+prompt)?)\s*[:：]\s*(.*))?$",
                     re.DOTALL | re.IGNORECASE)
_TAG_PART = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_ .'-]{0,79}|"
                       r"\([A-Za-z0-9_ .'-]{1,70}:\s*[0-9]+(?:\.[0-9]+)?\)|"
                       r"<lora:[A-Za-z0-9_. -]{1,80}(?::[-+]?[0-9]+(?:\.[0-9]+)?)?>)", re.IGNORECASE)
_TAG_MARKER = re.compile(r"(?:\([^()]+:\s*[0-9]+(?:\.[0-9]+)?\)|<lora:|\b[0-9]+(?:girl|boy|woman|man)s?\b)", re.IGNORECASE)


def _json(raw: str, fields: set[str], error: str) -> dict:
    if not isinstance(raw, str) or len(raw) > 8192:
        raise ValueError(error)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(error) from exc
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError(error)
    return data


def split_negative_marker(text: str) -> tuple[str, str]:
    if not isinstance(text, str) or len(text) > 2000:
        raise ValueError("drawing_intent_invalid")
    markers = list(_NEGATIVE_MARKER.finditer(text))
    if not markers:
        return text, ""
    positive = text[:markers[0].start()].strip(" \t\r\n,，;；")
    parts = [text[marker.end():markers[index + 1].start() if index + 1 < len(markers) else len(text)]
             .strip(" \t\r\n,，;；") for index, marker in enumerate(markers)]
    return positive, ", ".join(part for part in parts if part)


def _prompt_parts(text: str) -> tuple[str, str, str]:
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise ValueError("drawing_intent_invalid")
    positive_input, marked_negative = split_negative_marker(text)
    fields = _FIELDS.fullmatch(positive_input)
    if fields:
        raw_positive, negative = fields.group(1).strip(), (fields.group(2) or "").strip()
    else:
        negative_parts = []
        def remove_negative(match):
            negative_parts.append(match.group(1).strip())
            return ""
        raw_positive = _NEGATIVE.sub(remove_negative, positive_input).strip(" \t\r\n,，;；")
        negative = ", ".join(part for part in negative_parts if part)
    negative = ", ".join(part for part in (negative, marked_negative) if part)
    positive = _LORA_WORD.sub("", _LORA.sub("", raw_positive)).strip(" \t\r\n,，;；")
    negative = _LORA_WORD.sub("", _LORA.sub("", negative)).strip(" \t\r\n,，;；")
    return positive, negative, raw_positive


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _mentioned_keys(raw_positive: str, aliases: list[str]) -> tuple[str, ...]:
    text = _normalize_text(raw_positive)
    found, occupied = [], set()
    for alias in sorted(aliases, key=len, reverse=True):
        key = normalize_lora_key(alias)
        start = text.find(key)
        if start >= 0 and not any(index in occupied for index in range(start, start + len(key))):
            found.append(alias)
            occupied.update(range(start, start + len(key)))
    for match in _LORA.finditer(raw_positive):
        value = match.group(1).strip()
        if normalize_lora_key(value) not in {normalize_lora_key(item) for item in found}:
            found.append(value)
    for match in _LORA_WORD.finditer(_LORA.sub("", raw_positive)):
        value = match.group(1).strip()
        if normalize_lora_key(value) not in {normalize_lora_key(item) for item in found}:
            found.append(value)
    return tuple(found)


def explicit_prompt(text: str, aliases: list[str], *, allow_english: bool = False) -> PromptIntent | None:
    fields = _FIELDS if allow_english else _CHINESE_FIELDS
    if not isinstance(text, str) or not fields.fullmatch(text):
        return None
    positive, negative, raw_positive = _prompt_parts(text)
    if not (positive or negative):
        raise ValueError("drawing_intent_invalid")
    return PromptIntent(mode="direct", positive=positive, negative=negative,
                        lora_keys=_mentioned_keys(raw_positive, aliases))


def explicit_tag_prompt(text: str, aliases: list[str]) -> PromptIntent | None:
    if not isinstance(text, str) or len(text) > 2000:
        return None
    positive, _negative = split_negative_marker(text)
    tags = [part.strip() for part in re.split(r"[,，]", positive)]
    if not 3 <= len(tags) <= 100 or not _TAG_MARKER.search(positive) or any(not _TAG_PART.fullmatch(tag) for tag in tags):
        return None
    return decode_interpretation('{"mode":"direct","lora_keys":[]}', text, aliases)


def intent_failure_category(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return "empty"
    if len(raw) > 8192:
        return "too_long"
    try:
        data = json.loads(raw)
    except ValueError:
        return "invalid_json"
    if not isinstance(data, dict) or set(data) != {"mode", "lora_keys"}:
        return "fields"
    if not isinstance(data["mode"], str) or data["mode"] not in {"chat", "wish", "direct", "clarify"}:
        return "mode"
    if not isinstance(data["lora_keys"], list) or len(data["lora_keys"]) > 100:
        return "keys"
    return "conditions"


def decode_interpretation(raw: str, user_text: str, aliases: list[str]) -> PromptIntent:
    data = _json(raw, {"mode", "lora_keys"}, "drawing_intent_invalid")
    mode, keys = data["mode"], data["lora_keys"]
    if not isinstance(mode, str) or mode not in {"chat", "wish", "direct", "clarify"} or not isinstance(keys, list) or len(keys) > 100:
        raise ValueError("drawing_intent_invalid")
    positive, negative, raw_positive = _prompt_parts(user_text)
    positive_input, marked_negative = split_negative_marker(user_text)
    if mode in {"wish", "direct"} and not _FIELDS.fullmatch(positive_input):
        embedded = any(match.start() > 0 and positive_input[match.start() - 1] not in " \t\r\n,，;；"
                       for match in _NEGATIVE.finditer(positive_input))
        if embedded and mode == "direct":
            raise ValueError("drawing_negative_unclear")
        if embedded:
            negative = marked_negative
    mentioned = _mentioned_keys(raw_positive, aliases)
    allowed = {normalize_lora_key(value) for value in mentioned}
    try:
        normalized = [normalize_lora_key(value) for value in keys]
    except (TypeError, ValueError) as exc:
        raise ValueError("drawing_intent_invalid") from exc
    positive_text = _normalize_text(raw_positive)
    explicit_lora = bool(re.search(r"(?<![a-z0-9_])lora(?![a-z0-9_])", positive_text))
    if explicit_lora:
        allowed.update(key for key in normalized if key in positive_text)
    if len(normalized) != len(set(normalized)):
        raise ValueError("drawing_intent_invalid")
    if mode in {"chat", "clarify"} and keys:
        raise ValueError("drawing_intent_invalid")
    if mode == "direct" and not (positive or negative):
        raise ValueError("drawing_intent_invalid")
    resolved = list(mentioned)
    known = {normalize_lora_key(value) for value in resolved}
    for key, value in zip(normalized, keys):
        if key not in allowed:
            if not explicit_lora and key in positive_text:
                continue
            raise ValueError("drawing_intent_invalid")
        if key not in known:
            resolved.append(value)
            known.add(key)
    return PromptIntent(mode=mode, positive=positive if mode == "direct" else "",
                        negative=negative if mode in {"direct", "wish"} else "",
                        lora_keys=tuple(resolved) if mode in {"direct", "wish"} else ())


def decode_wish(raw: str, required_negative: str = "") -> tuple[str, str]:
    data = _json(raw, {"positive", "negative"}, "drawing_wish_invalid")
    positive, negative = data["positive"], data["negative"]
    if (not isinstance(positive, str) or not isinstance(negative, str)
            or not positive.strip() or len(positive) + len(negative) > 2000
            or "<lora" in positive.casefold() or "<lora" in negative.casefold()
            or _LORA_WORD.search(positive) or _LORA_WORD.search(negative)
            or not isinstance(required_negative, str)):
        raise ValueError("drawing_wish_invalid")
    positive, negative, required_negative = positive.strip(), negative.strip(), required_negative.strip()
    if required_negative and required_negative not in negative:
        negative = ", ".join(value for value in (negative, required_negative) if value)
    if len(positive) + len(negative) > 2000:
        raise ValueError("drawing_wish_invalid")
    return positive, negative
