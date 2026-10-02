import json
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from al1s.execution.errors import InvalidRequestError

ASSETS = Path(__file__).parent / "assets"
MODEL_VERSION = "lineup-portrait-yolov8n-ppocrv5-v4"
SUPPORTED_MODEL_VERSIONS = {
    MODEL_VERSION,
    "lineup-portrait-yolov8n-ppocrv5-v3",
    "lineup-portrait-yolov8n-ppocrv5-v2",
    "lineup-portrait-yolov8n-v1",
}
CAPABILITY = "lineup-recognition-v1"


@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((ASSETS / "catalog.json").read_text(encoding="utf-8")))


def validate_review(ids: list[int | None]) -> None:
    valid = {student["id"] for student in catalog()["students"]}
    if len(ids) != 12 or any(
        type(sid) is not int or sid not in valid for sid in ids if sid is not None
    ):
        raise InvalidRequestError("lineup_review_invalid", "请为十二个位置选择有效学生")
    for team in (ids[:6], ids[6:]):
        present = [sid for sid in team if sid is not None]
        if len(set(present)) != len(present):
            raise InvalidRequestError(
                "lineup_duplicate_student", "同一阵容不能重复选择同一个学生 ID"
            )
