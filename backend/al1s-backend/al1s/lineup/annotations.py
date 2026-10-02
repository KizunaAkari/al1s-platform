"""Human labels in original pixels, independent from inference success."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from al1s.execution.errors import InvalidRequestError
from al1s.lineup.catalog import catalog

Side = Literal["attack", "defense"]


class Region(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["portrait", "name"]
    box: tuple[StrictInt, StrictInt, StrictInt, StrictInt]


class Slot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Side
    index: StrictInt = Field(ge=0, le=5)
    student_id: StrictInt | None = None
    regions: list[Region] = Field(default_factory=list, max_length=2)


class AnnotationDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    teams: dict[Side, StrictInt] = Field(default_factory=dict)
    slots: list[Slot] = Field(default_factory=list, max_length=12)


def validate_annotation(payload: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    try:
        doc = AnnotationDocument.model_validate(payload)
    except ValidationError as exc:
        raise InvalidRequestError("lineup_annotation_invalid", "标注格式或原图坐标无效") from exc
    valid_ids = {s["id"] for s in catalog()["students"]}
    seen: set[tuple[str, int]] = set()
    students: set[tuple[str, int]] = set()
    complete = bool(doc.teams)
    if any(not 1 <= count <= 6 for count in doc.teams.values()):
        raise InvalidRequestError("lineup_annotation_invalid", "每侧人数必须为 1 至 6")
    for slot in doc.slots:
        key = (slot.side, slot.index)
        if key in seen or slot.index >= doc.teams.get(slot.side, 0):
            raise InvalidRequestError("lineup_annotation_invalid", "标注位置重复或不属于已有阵容")
        seen.add(key)
        if slot.student_id is not None:
            student = (slot.side, slot.student_id)
            if slot.student_id not in valid_ids or student in students:
                raise InvalidRequestError("lineup_annotation_invalid", "学生 ID 无效或侧内重复")
            students.add(student)
        kinds: set[str] = set()
        for region in slot.regions:
            x, y, w, h = region.box
            if region.kind in kinds or min(x, y) < 0 or min(w, h) <= 0:
                raise InvalidRequestError("lineup_annotation_invalid", "区域重复或坐标无效")
            if x + w > width or y + h > height:
                raise InvalidRequestError("lineup_annotation_invalid", "标注超出原图")
            kinds.add(region.kind)
        complete = complete and slot.student_id is not None and bool(slot.regions)
    complete = complete and len(seen) == sum(doc.teams.values())
    result = doc.model_dump(mode="json")
    result["slots"].sort(key=lambda s: (s["side"] != "attack", s["index"]))
    return {**result, "state": "confirmed" if complete else "draft"}
