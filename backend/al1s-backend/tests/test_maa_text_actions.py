import pytest

from al1s.maa.compiler import MaaActionCompilerRegistry
from al1s.maa.validation import validate_script_document


@pytest.mark.parametrize("action", ["wait_text", "click_text"])
def test_text_actions_validate_and_require_ocr(action):
    document = {
        "version": 2,
        "script_type": "standard",
        "target": {"application_package": "com.example"},
        "steps": [{"action": action, "text": "领取[奖励]+", "timeout_seconds": 30}],
    }
    result = validate_script_document(document)
    assert result.valid, result.issues
    assert MaaActionCompilerRegistry().require(action).provider_keys == ("ocr",)
    for text in ("", " " * 3, "x" * 201, 123):
        document["steps"][0]["text"] = text
        assert not validate_script_document(document).valid


def test_text_action_rejects_bad_region_and_poll_interval():
    document = {
        "version": 2,
        "script_type": "standard",
        "target": {
            "application_package": "com.example",
            "screen_size": {"width": 64, "height": 96},
        },
        "steps": [
            {
                "action": "click_text",
                "text": "OK",
                "poll_interval_seconds": 0,
                "search_region": {"x": 60, "y": 0, "width": 20, "height": 10},
            }
        ],
    }
    issues = validate_script_document(document).issues
    assert any(item.code == "coordinate_out_of_screen" for item in issues)
    assert any("poll_interval_seconds" in item.pointer for item in issues)


def test_ocr_assertion_validates_text_mode_region_and_legacy_image():
    assertion = {
        "enabled": True,
        "recognition_mode": "text",
        "text": "开始[游戏]+",
        "timeout_seconds": 20,
        "poll_interval_seconds": 1,
        "max_retries": 2,
        "search_region": {"x": 1, "y": 2, "width": 30, "height": 40},
    }
    document = {
        "version": 2,
        "script_type": "module_process",
        "target": {"application_package": "com.example"},
        "steps": [{"action": "wait", "seconds": 1, "post_assertion": assertion}],
    }
    assert validate_script_document(document).valid
    for text in ("", "   ", "x" * 201, 123):
        assertion["text"] = text
        assert not validate_script_document(document).valid
    assertion["text"] = "OK"
    assertion["search_region"]["width"] = 0
    assert not validate_script_document(document).valid
    assertion["search_region"]["width"] = 30
    assertion["recognition_mode"] = "unsupported"
    assert not validate_script_document(document).valid
    assertion.pop("recognition_mode")
    assertion["template_base64"] = {
        "$blob": "11111111-1111-4111-8111-111111111111",
        "sha256": "a" * 64,
        "size_bytes": 10,
        "media_type": "image/png",
    }
    assertion["threshold"] = 0.85
    assert validate_script_document(document).valid
