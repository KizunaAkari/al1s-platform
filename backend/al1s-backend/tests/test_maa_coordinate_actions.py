import pytest

from al1s.maa.validation import validate_script_document


@pytest.mark.parametrize(
    "step",
    [
        {"action": "tap", "x": 10, "y": 20},
        {"action": "swipe", "x1": 0, "y1": 0, "x2": 63, "y2": 95, "duration_ms": 300},
    ],
)
def test_coordinate_actions_require_native_size_and_valid_coordinates(step):
    doc = {
        "version": 2,
        "script_type": "module_process",
        "steps": [step],
        "target": {"application_package": "com.example"},
    }
    assert not validate_script_document(doc).valid
    doc["target"]["screen_size"] = {"width": 64, "height": 96}
    assert validate_script_document(doc).valid
    step["x" if step["action"] == "tap" else "x2"] = 64
    assert "coordinate_out_of_screen" in {i.code for i in validate_script_document(doc).issues}


def test_tap_does_not_accept_hidden_shell_or_unimplemented_repeat():
    doc = {
        "version": 2,
        "script_type": "module_process",
        "target": {
            "application_package": "com.example",
            "screen_size": {"width": 64, "height": 96},
        },
        "steps": [{"action": "tap", "x": 0, "y": 0, "command_line": "bad"}],
    }
    assert not validate_script_document(doc).valid
