"""Validate terminal evidence before exposing automatic lineup decisions."""

import math
from typing import Any

from al1s.lineup.catalog import MODEL_VERSION, SUPPORTED_MODEL_VERSIONS, catalog
from al1s.lineup.decision import decision
from al1s.lineup.identity import ambiguous_ids


def validated_result(raw: Any, record: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if (
        not isinstance(raw.get("model_version"), str)
        or raw.get("model_version") not in SUPPORTED_MODEL_VERSIONS
        or raw.get("catalog_version") != record["catalog_version"]
    ):
        return None
    if raw.get("model_version") == "lineup-portrait-yolov8n-ppocrv5-v2" and (
        raw.get("attack_side") not in ("left", "right", None)
        or (raw.get("layout_valid") and raw.get("attack_side") is None)
    ):
        return None
    v4 = raw["model_version"] == MODEL_VERSION
    modern = v4 or raw["model_version"].endswith("-v3")
    teams: list[str] = raw.get("teams", [])
    if modern and not valid_teams(raw):
        return None
    sizes = raw.get("team_sizes", {}) if v4 else {side: 6 for side in teams}
    if v4 and (
        not isinstance(sizes, dict)
        or set(sizes) != set(teams)
        or any(type(n) is not int or not 1 <= n <= 6 for n in sizes.values())
    ):
        return None
    slots = raw.get("slots")
    if not isinstance(slots, list) or len(slots) != 12 or type(raw.get("layout_valid")) is not bool:
        return None
    valid_ids = {s["id"] for s in catalog()["students"]}
    checked = []
    for index, slot in enumerate(slots):
        if (
            not isinstance(slot, dict)
            or not {"image_id", "ocr_id"}.issubset(slot)
            or slot.get("side") != ("attack" if index < 6 else "defense")
            or slot.get("index") != index % 6
        ):
            return None
        if any(
            slot.get(key) is not None and (type(slot[key]) is not int or slot[key] not in valid_ids)
            for key in ("image_id", "ocr_id")
        ):
            return None
        if not isinstance(slot.get("ocr_text"), str) or len(slot["ocr_text"]) > 200:
            return None
        values: list[Any] = [slot.get(k) for k in ("score", "margin", "ocr_score")]
        if any(type(v) not in (float, int) or not math.isfinite(v) for v in values):
            return None
        box = slot.get("box")
        if box is not None:
            if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
                return None
            x, y, w, h = box
            if (
                min(x, y) < 0
                or min(w, h) <= 0
                or x + w > record["width"]
                or y + h > record["height"]
            ):
                return None
        item = dict(slot)
        if modern:
            present = slot.get("present")
            if type(present) is not bool or present != (slot["index"] < sizes.get(slot["side"], 0)):
                return None
            if present != (box is not None):
                return None
            if not present and any(slot[k] is not None for k in ("image_id", "ocr_id")):
                return None
            item["ambiguous_ids"] = (
                ambiguous_ids(slot["image_id"], slot["ocr_text"], catalog()["students"])
                if v4
                else []
            )
            item.update(decision(item, raw["layout_valid"]))
            checked.append(item)
            continue
        item["agreed"] = bool(
            raw["layout_valid"]
            and box is not None
            and slot["image_id"] is not None
            and slot["image_id"] == slot["ocr_id"]
            and values[0] >= 0.72
            and values[1] >= 0.08
            and values[2] >= 0.75
        )
        checked.append(item)
    return {**raw, "slots": checked}


def valid_teams(raw: dict[str, Any]) -> bool:
    teams = raw.get("teams")
    if teams not in ([], ["attack"], ["defense"], ["attack", "defense"]):
        return False
    if bool(teams) != raw.get("layout_valid"):
        return False
    hint, mode = raw.get("layout_hint"), raw.get("recognition_mode")
    if hint not in ("auto", "attack", "defense", "left_attack", "right_attack"):
        return False
    if mode not in ("auto", "portrait", "text"):
        return False
    if not teams:
        return True
    if hint in ("attack", "defense"):
        return bool(teams == [hint])
    return bool(teams == ["attack", "defense"] and raw.get("attack_side") in ("left", "right"))
