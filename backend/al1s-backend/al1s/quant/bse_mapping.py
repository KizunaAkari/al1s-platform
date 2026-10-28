"""Historical exchange-issued code switch, without guessing code suffixes."""

import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

SWITCH_DATE = date(2025, 10, 9)


@lru_cache(maxsize=1)
def _mapping() -> tuple[dict[str, str], dict[str, str]]:
    source = json.loads(
        (Path(__file__).parent / "data" / "bse_2025_mapping.json").read_text(encoding="utf-8")
    )
    if source["effective_date"] != SWITCH_DATE.isoformat() or len(source["entries"]) != 248:
        raise ValueError("quant_bse_mapping_invalid")
    old_to_new, new_to_old = {}, {}
    for row in source["entries"]:
        old, current = row["old"], row["current"]
        if (
            not re.fullmatch(r"[0-9]{6}", old)
            or not re.fullmatch(r"920[0-9]{3}", current)
            or old in old_to_new
            or current in new_to_old
        ):
            raise ValueError("quant_bse_mapping_invalid")
        old_to_new[old], new_to_old[current] = current, old
    return old_to_new, new_to_old


def code_for_date(code: str, day: date) -> str:
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) or type(day) is not date:
        raise ValueError("quant_code_invalid")
    old_to_new, new_to_old = _mapping()
    if day >= SWITCH_DATE:
        if code in old_to_new:
            return old_to_new[code]
        if code.startswith(("4", "8")):
            raise ValueError("quant_code_mapping_unverified")
        return code
    return new_to_old.get(code, code)
