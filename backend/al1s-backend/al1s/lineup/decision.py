"""Versioned confidence policy; keep backend validation in sync."""

from typing import Any


def decision(slot: dict[str, Any], layout_valid: bool) -> dict[str, Any]:
    result = dict(selected_id=None, accepted=False, agreed=False, evidence="unresolved")
    if not slot.get("present", True):
        return {**result, "evidence": "absent"}
    # Shared names alone cannot establish the user-confirmed Hoshino form.
    if len(slot.get("ambiguous_ids", [])) > 1 and not (
        set(slot["ambiguous_ids"]) == {10098, 10099}
        and slot["image_id"] in (10098, 10099)
        and slot["score"] >= 0.90
        and slot["margin"] >= 0.15
    ):
        return result
    image_id, text_id = slot["image_id"], slot["ocr_id"]
    if image_id is not None and text_id is not None:
        if image_id != text_id:
            return {**result, "evidence": "conflict"}
        if (
            layout_valid
            and slot["score"] >= 0.72
            and slot["margin"] >= 0.08
            and slot["ocr_score"] >= 0.75
        ):
            return dict(selected_id=image_id, accepted=True, agreed=True, evidence="dual")
        return result
    if layout_valid and image_id is not None and slot["score"] >= 0.80 and slot["margin"] >= 0.10:
        return dict(selected_id=image_id, accepted=True, agreed=False, evidence="portrait_only")
    if layout_valid and text_id is not None and slot["ocr_score"] >= 0.90:
        return dict(selected_id=text_id, accepted=True, agreed=False, evidence="text_only")
    return result
