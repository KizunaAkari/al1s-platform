import pytest

from al1s.execution.errors import InvalidRequestError
from al1s.lineup.annotations import validate_annotation
from al1s.lineup.attention import attention_reason, effective_lineup
from al1s.lineup.catalog import catalog


def document():
    return {
        "teams": {"attack": 1},
        "slots": [
            {
                "side": "attack",
                "index": 0,
                "student_id": catalog()["students"][0]["id"],
                "regions": [{"kind": "portrait", "box": [10, 20, 30, 40]}],
            }
        ],
    }


def test_failed_image_can_be_annotated_without_a_machine_result():
    result = validate_annotation(document(), 100, 100)
    assert result["state"] == "confirmed"
    assert result["slots"][0]["regions"][0]["box"] == [10, 20, 30, 40]
    assert attention_reason({"state": "failure", "result": None, "annotation": result}) == "none"


def test_missing_label_or_region_is_a_saved_draft_not_a_usable_result():
    payload = document()
    payload["slots"][0]["student_id"] = None
    assert validate_annotation(payload, 100, 100)["state"] == "draft"
    payload = document()
    payload["slots"][0]["regions"] = []
    assert validate_annotation(payload, 100, 100)["state"] == "draft"
    assert validate_annotation({"teams": {}, "slots": []}, 100, 100)["state"] == "draft"


@pytest.mark.parametrize(
    "box", [[-1, 0, 1, 1], [90, 0, 20, 1], [0, 0, 0, 1], [1.1, 0, 2, 2], [True, 0, 2, 2]]
)
def test_rejects_invalid_original_pixel_boxes(box):
    payload = document()
    payload["slots"][0]["regions"][0]["box"] = box
    with pytest.raises(InvalidRequestError):
        validate_annotation(payload, 100, 100)


def test_duplicate_positions_students_or_region_kinds_are_rejected():
    for change in ("position", "student", "region"):
        payload = document()
        if change == "region":
            payload["slots"][0]["regions"] *= 2
        else:
            import copy

            payload["teams"]["attack"] = 2
            slot = copy.deepcopy(payload["slots"][0])
            if change == "student":
                slot["index"] = 1
            payload["slots"].append(slot)
        with pytest.raises(InvalidRequestError):
            validate_annotation(payload, 100, 100)


def test_failure_and_unresolved_are_attention_but_running_and_cancelled_are_not():
    assert attention_reason({"state": "failure"}) == "runtime_failure"
    assert attention_reason({"state": "timed_out"}) == "runtime_failure"
    assert attention_reason({"state": "running"}) == "none"
    assert attention_reason({"state": "cancelled"}) == "none"
    assert attention_reason({"state": "success", "result": None}) == "layout_invalid"
    result = {
        "layout_valid": True,
        "teams": ["attack"],
        "team_sizes": {"attack": 1},
        "slots": [
            {
                "side": "attack",
                "index": 0,
                "present": True,
                "accepted": False,
                "evidence": "conflict",
            }
        ],
    }
    assert attention_reason({"state": "success", "result": result}) == "conflict"
    result["slots"][0].update(accepted=True, selected_id=catalog()["students"][0]["id"])
    assert attention_reason({"state": "success", "result": result}) == "none"
    assert (
        attention_reason({"state": "success", "result": result, "review": [None] * 12})
        == "unresolved"
    )


def test_saved_incomplete_human_annotation_is_not_replaced_by_machine_success():
    item = {
        "state": "success",
        "result": {"layout_valid": True},
        "annotation": {"state": "draft", "teams": {}, "slots": []},
    }
    assert attention_reason(item) == "unresolved"
    assert effective_lineup(item) is None
