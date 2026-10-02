from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from al1s.execution.definitions import canonical_manifest_hash
from al1s.maa.compiler import MaaActionCompilerRegistry, MaaRegisteredActionCompiler
from al1s.maa.types import ScriptVersionRecord
from al1s.maa.validation import validate_script_document


def document(step: dict) -> dict:
    return {
        "version": 2,
        "script_type": "module_process",
        "target": {
            "application_package": "com.example",
            "screen_size": {"width": 1080, "height": 1920},
        },
        "steps": [step],
    }


def test_random_wait_range_is_bounded_by_step_timeout() -> None:
    good = document(
        {"action": "wait_random", "min_seconds": 1, "max_seconds": 3, "timeout_seconds": 5}
    )
    assert validate_script_document(good).valid
    for low, high, timeout in [(3, 1, 5), (1, 6, 5), (-1, 2, 5)]:
        bad = document(
            {
                "action": "wait_random",
                "min_seconds": low,
                "max_seconds": high,
                "timeout_seconds": timeout,
            }
        )
        assert not validate_script_document(bad).valid
    assert (
        MaaActionCompilerRegistry().require("wait_random").handler_id
        == "maa.registered.wait_random"
    )


def test_recognition_execution_requires_targets_and_native_size() -> None:
    text = document(
        {
            "action": "recognize_execute",
            "recognition_mode": "text",
            "text": "领取[奖励]",
            "execution_mode": "fixed_swipe",
            "swipe": {"x1": 10, "y1": 20, "x2": 300, "y2": 400, "duration_ms": 350},
            "execution_count": 3,
        }
    )
    assert validate_script_document(text).valid
    text["target"].pop("screen_size")
    assert any(
        issue.code == "native_screen_size_required"
        for issue in validate_script_document(text).issues
    )
    text["target"]["screen_size"] = {"width": 1080, "height": 1920}
    text["steps"][0]["execution_count"] = 0
    assert not validate_script_document(text).valid

    image = document(
        {
            "action": "recognize_execute",
            "recognition_mode": "image",
            "template_base64": "data:image/png;base64,YQ==",
            "execution_mode": "image_center",
        }
    )
    assert any(
        issue.code == "click_template_missing"
        for issue in validate_script_document(image, allow_inline_resources=True).issues
    )


def test_new_handlers_and_conditional_ocr_capability_compile() -> None:
    manifest = document(
        {
            "action": "recognize_execute",
            "recognition_mode": "text",
            "text": "领取",
            "execution_mode": "fixed_tap",
            "click": {"x": 10, "y": 20},
        }
    )
    manifest["steps"].append({"action": "task_view"})
    manifest["steps"].append(
        {"action": "wait_random", "min_seconds": 1, "max_seconds": 2, "timeout_seconds": 5}
    )
    version = ScriptVersionRecord(
        script_version_id=uuid4(),
        script_id=uuid4(),
        revision=1,
        schema_version=2,
        manifest_hash=canonical_manifest_hash(manifest),
        manifest=manifest,
        created_at=datetime.now(UTC),
    )
    compiled = MaaRegisteredActionCompiler().compile(version, [])
    assert compiled.provider_keys == ("maa", "ocr")
    assert [step["handler_id"] for step in compiled.definition["steps"]] == [
        "maa.pipeline.recognize_execute",
        "maa.pipeline.task_view",
        "maa.registered.wait_random",
    ]
