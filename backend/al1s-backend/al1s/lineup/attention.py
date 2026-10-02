"""A single decision for retry eligibility, selectors and usable results."""

from typing import Any


def effective_lineup(detail: dict[str, Any]) -> dict[str, Any] | None:
    annotation = detail.get("annotation") or {}
    if annotation.get("state") == "confirmed":
        teams = annotation["teams"]
        slots = []
        for slot in annotation["slots"]:
            region = next(iter(slot["regions"]), None)
            slots.append(
                {
                    **slot,
                    "present": True,
                    "selected_id": slot["student_id"],
                    "accepted": True,
                    "box": region["box"] if region else None,
                }
            )
        return {
            "teams": list(teams),
            "team_sizes": teams,
            "slots": slots,
            "layout_valid": True,
            "source": "annotation",
        }
    if annotation:
        return None
    return detail.get("result") if detail.get("state") == "success" else None


def attention_reason(detail: dict[str, Any]) -> str:
    if (detail.get("annotation") or {}).get("state") == "confirmed":
        return "none"
    state = detail.get("state")
    if state in {"failure", "timed_out"}:
        return "runtime_failure"
    if state != "success":
        return "none"
    if detail.get("annotation"):
        return "unresolved"
    result = detail.get("result")
    if not result or not result.get("layout_valid"):
        return "layout_invalid"
    teams = result.get("teams", ["attack", "defense"])
    if not teams:
        return "layout_invalid"
    review = detail.get("review") or []
    explicit_review = detail.get("review") is not None
    slots = {(s["side"], s["index"]): s for s in result.get("slots", [])}
    for side in teams:
        selected: set[int] = set()
        for index in range(result.get("team_sizes", {}).get(side, 6)):
            slot = slots.get((side, index), {})
            position = index + (6 if side == "defense" else 0)
            sid = review[position] if position < len(review) else None
            if sid is None and not explicit_review:
                if slot.get("accepted"):
                    sid = slot.get("selected_id")
                elif slot.get("agreed"):
                    sid = slot.get("image_id")
            if sid is None or sid in selected:
                return (
                    "conflict"
                    if not explicit_review and slot.get("evidence") == "conflict"
                    else "unresolved"
                )
            selected.add(sid)
    return "none"
