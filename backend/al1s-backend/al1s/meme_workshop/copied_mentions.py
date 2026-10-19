import random
import re
import unicodedata
from collections.abc import Callable, Sequence
from typing import Any

MAX_MEMBERS = 10_000
MAX_TARGETS = 4


class CopiedMentionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    @property
    def reply_text(self) -> str:
        if self.code == "copied_target_self_only":
            return "复制的@只匹配到你自己。请用蓝色@选择他人。想摸自己可直接发“摸摸”。"
        return "无法确认复制的@对象。请从QQ弹出的群成员列表选择蓝色@后再发。"


def normalized_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def member_names(members: Sequence[dict[str, Any]]) -> dict[str, set[str]]:
    # One complete roster snapshot; never silently discard malformed entries or truncate it.
    if len(members) > MAX_MEMBERS:
        raise CopiedMentionError("copied_members_unavailable")
    names: dict[str, set[str]] = {}
    displays: dict[str, str] = {}
    for member in members:
        value = member.get("user_id")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise CopiedMentionError("copied_members_unavailable")
        user = str(value)
        card, nickname = member.get("card"), member.get("nickname")
        card = "" if card is None else card
        nickname = "" if nickname is None else nickname
        if not re.fullmatch(r"[1-9][0-9]{0,19}", user) or any(
            not isinstance(name, str) or len(name) > 256 for name in (card, nickname)
        ):
            raise CopiedMentionError("copied_members_unavailable")
        display = normalized_name(card) or normalized_name(nickname)
        if user in displays and displays[user] != display:
            raise CopiedMentionError("copied_members_unavailable")
        displays[user] = display
        if display:
            names.setdefault(display, set()).add(user)
    return names


def candidate_groups(text: str, names: dict[str, set[str]]) -> tuple[tuple[str, ...], ...]:
    text = normalized_name(text)
    if not text.startswith("@") or len(text) > 200:
        raise CopiedMentionError("copied_targets_invalid")
    boundaries = list(re.finditer(r"\s+@", text))
    if len(boundaries) >= MAX_TARGETS:
        raise CopiedMentionError("copied_targets_exceeded")
    interpretations: set[tuple[tuple[str, ...], ...]] = set()
    for mask in range(1 << len(boundaries)):
        start, parts = 1, []
        for index, boundary in enumerate(boundaries):
            if mask & (1 << index):
                parts.append(text[start : boundary.start()].strip())
                start = boundary.end()
        parts.append(text[start:].strip())
        if all(part in names for part in parts):
            interpretations.add(tuple(tuple(sorted(names[part])) for part in parts))
    if not interpretations:
        raise CopiedMentionError("copied_targets_not_found")
    if len(interpretations) != 1:
        raise CopiedMentionError("copied_targets_ambiguous")
    return next(iter(interpretations))


def resolve_copied_targets(
    text: str,
    members: Sequence[dict[str, Any]],
    sender_id: str,
    *,
    choose: Callable[[Sequence[str]], str] | None = None,
) -> list[str]:
    pick = choose or random.SystemRandom().choice
    groups = candidate_groups(text, member_names(members))
    result = []
    for group in groups:
        others = [user for user in group if user != sender_id]
        if not others:
            raise CopiedMentionError("copied_target_self_only")
        result.append(pick(others))
    return result
