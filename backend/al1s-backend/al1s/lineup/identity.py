"""Conservative identity rules for indistinguishable catalog forms."""

import re
import unicodedata
from typing import Any


def normalized_name(text: str) -> str:
    return re.sub(r"[\s()\[\]·・]", "", unicodedata.normalize("NFKC", text)).casefold()


def ambiguous_ids(image_id: int | None, text: str, students: list[dict[str, Any]]) -> list[int]:
    """Shared visible names cannot establish a form ID independently."""
    aliases = {normalized_name(text)} if text else set()
    aliases.update(
        normalized_name(a) for s in students if s["id"] == image_id for a in s["aliases"]
    )
    matches = {
        s["id"] for s in students if any(normalized_name(a) in aliases for a in s["aliases"])
    }
    return sorted(matches) if 1 < len(matches) <= 6 else []
