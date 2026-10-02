"""Check absolute coordinates against a script's immutable native screen size."""

from typing import Any, TypeGuard

from al1s.maa.types import ValidationIssue

RECT_FIELDS = {
    "template_rect",
    "preview_rect",
    "click_template_rect",
    "search_region",
    "click_search_region",
    "region",
}


def _native_number(value: object) -> TypeGuard[int | float]:
    """Accept built-in numbers but not bool or numeric subclasses."""
    return type(value) in (int, float)


def validate_coordinates(
    value: Any, width: int, height: int, issues: list[ValidationIssue], pointer: str = ""
) -> None:
    if isinstance(value, list):
        for index, child in enumerate(value):
            validate_coordinates(child, width, height, issues, f"{pointer}/{index}")
    elif isinstance(value, dict):
        if value.get("action") in {"tap", "swipe"}:
            coordinates = (
                (("x", "y"),) if value["action"] == "tap" else (("x1", "y1"), ("x2", "y2"))
            )
            for x_key, y_key in coordinates:
                x, y = value.get(x_key), value.get(y_key)
                if (
                    _native_number(x)
                    and _native_number(y)
                    and not (0 <= x < width and 0 <= y < height)
                ):
                    issues.append(
                        ValidationIssue(
                            "coordinate_out_of_screen",
                            "Point exceeds the bound native screen",
                            pointer,
                        )
                    )
                    break
        for key, child in value.items():
            path = f"{pointer}/{key.replace('~', '~0').replace('/', '~1')}"
            if isinstance(child, dict):
                points = []
                if key in RECT_FIELDS:
                    x, y, w, h = (child.get(k) for k in ("x", "y", "width", "height"))
                    if (
                        _native_number(x)
                        and _native_number(y)
                        and _native_number(w)
                        and _native_number(h)
                        and not (
                            0 <= x < width
                            and 0 <= y < height
                            and w > 0
                            and h > 0
                            and x + w <= width
                            and y + h <= height
                        )
                    ):
                        issues.append(
                            ValidationIssue(
                                "coordinate_out_of_screen",
                                "Region exceeds the bound native screen",
                                path,
                            )
                        )
                elif key == "click":
                    points = [(child.get("x"), child.get("y"))]
                elif key == "swipe":
                    points = [
                        (child.get("x1"), child.get("y1")),
                        (child.get("x2"), child.get("y2")),
                    ]
                for x, y in points:
                    if (
                        _native_number(x)
                        and _native_number(y)
                        and not (0 <= x < width and 0 <= y < height)
                    ):
                        issues.append(
                            ValidationIssue(
                                "coordinate_out_of_screen",
                                "Point exceeds the bound native screen",
                                path,
                            )
                        )
                        break
            validate_coordinates(child, width, height, issues, path)
