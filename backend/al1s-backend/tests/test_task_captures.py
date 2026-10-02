from uuid import uuid4

from al1s.execution.task_captures import task_captures


def test_only_screenshot_reference_fields_escape_projection_and_duplicates_collapse():
    identity = uuid4()
    capture = {
        "artifact_kind": "screenshot",
        "artifact_id": str(identity),
        "file_name": "step.png",
        "path": "private",
        "data_base64": "private",
    }
    diagnostic = {
        "custom_actions": [{"result": capture}],
        "duplicate": capture,
        "video": {**capture, "artifact_kind": "video", "artifact_id": str(uuid4())},
    }
    result = task_captures(diagnostic)
    assert len(result) == 1 and result[0].artifact_id == identity
    assert result[0].file_name == "step.png" and "private" not in str(result)


def test_invalid_identity_is_ignored_and_later_module_captures_are_visible():
    items = [{"artifact_kind": "screenshot", "artifact_id": str(uuid4())} for _ in range(150)]
    assert len(task_captures({"captures": items})) == 150
    assert task_captures({"capture": {"artifact_kind": "screenshot", "artifact_id": "bad"}}) == ()
