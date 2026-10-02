from datetime import UTC, datetime
from uuid import uuid4

from al1s.execution.definitions import canonical_manifest_hash
from al1s.maa.compiler import MaaRegisteredActionCompiler
from al1s.maa.types import ScriptVersionRecord
from al1s.maa.validation import validate_script_document


def document(mode="image"):
    return {
        "version": 2,
        "script_type": "module_process",
        "target": {
            "application_package": "com.example",
            "screen_size": {"width": 96, "height": 64},
        },
        "steps": [
            {
                "action": "recognize_execute",
                "recognition_mode": mode,
                "execution_mode": "match_center",
                "template_base64": "data:image/png;base64,YQ==",
                "text": "领取",
                "execution_count": 3,
            }
        ],
    }


def test_reuses_only_the_recognition_template_and_marks_the_runtime_capability():
    manifest = document()
    assert validate_script_document(manifest, allow_inline_resources=True).valid
    version = ScriptVersionRecord(
        uuid4(), uuid4(), 1, 2, canonical_manifest_hash(manifest), manifest, datetime.now(UTC)
    )
    compiled = MaaRegisteredActionCompiler().compile(version, []).definition["steps"][0]
    assert compiled["parameters"]["execution_mode"] == "match_center"
    assert "click_template_base64" not in compiled["parameters"]
    assert any(
        item["handler_id"] == "maa.wrapper.recognize_match_center" for item in compiled["wrappers"]
    )


def test_ocr_cannot_silently_use_the_image_reuse_mode():
    issues = validate_script_document(document("text"), allow_inline_resources=True).issues
    assert any(
        item.code == "match_center_requires_image" and item.pointer == "/steps/0/execution_mode"
        for item in issues
    )
