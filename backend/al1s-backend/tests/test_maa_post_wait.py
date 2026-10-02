from datetime import UTC, datetime
from uuid import uuid4

import pytest

from al1s.execution.definitions import canonical_manifest_hash
from al1s.maa.compiler import (
    MaaActionCompilerRegistry,
    MaaActionDefinition,
    MaaRegisteredActionCompiler,
)
from al1s.maa.types import ScriptVersionRecord
from al1s.maa.validation import validate_script_document


def document(action: str, **extra: object) -> dict:
    step = {
        "action": action,
        **{
            "recognize_execute": {
                "recognition_mode": "text",
                "text": "领取",
                "execution_mode": "fixed_tap",
                "click": {"x": 10, "y": 20},
            },
            "wait_click": {
                "template_base64": "data:image/png;base64,YQ==",
                "click_mode": "match_center",
            },
            "wait_image": {"template_base64": "data:image/png;base64,YQ=="},
            "wait_text": {"text": "领取"},
            "click_text": {"text": "领取"},
            "smart_swipe": {
                "template_base64": "data:image/png;base64,YQ==",
                "swipe": {"x1": 10, "y1": 20, "x2": 30, "y2": 40, "duration_ms": 350},
            },
        }.get(action, {}),
        **extra,
    }
    return {
        "version": 2,
        "script_type": "module_process",
        "target": {
            "application_package": "com.example",
            "screen_size": {"width": 1080, "height": 1920},
        },
        "steps": [step],
    }


@pytest.mark.parametrize(
    "action",
    ["recognize_execute", "wait_click", "wait_image", "wait_text", "click_text", "smart_swipe"],
)
def test_wait_is_optional_bounded_numeric_and_specific_to_recognition(action):
    for value in [None, 0, 1.25, 14400]:
        assert validate_script_document(
            document(action, wait_after_execution_seconds=value), allow_inline_resources=True
        ).valid
    for value in [-1, 14400.001, "1", True, float("inf"), float("nan")]:
        issues = validate_script_document(
            document(action, wait_after_execution_seconds=value), allow_inline_resources=True
        ).issues
        assert any(issue.pointer == "/steps/0/wait_after_execution_seconds" for issue in issues)


def test_non_recognition_step_rejects_wait_parameter():
    assert any(
        issue.code == "unknown_action_field"
        for issue in validate_script_document(
            document("wait", seconds=1, wait_after_execution_seconds=1)
        ).issues
    )


@pytest.mark.parametrize("seconds", [None, 0, 1.25, 14400])
def test_compilation_requires_new_wrapper_only_for_nonzero_wait(seconds):
    manifest = document("wait_text", wait_after_execution_seconds=seconds)
    version = ScriptVersionRecord(
        script_version_id=uuid4(),
        script_id=uuid4(),
        revision=1,
        schema_version=2,
        manifest_hash=canonical_manifest_hash(manifest),
        manifest=manifest,
        created_at=datetime.now(UTC),
    )
    step = MaaRegisteredActionCompiler().compile(version, []).definition["steps"][0]
    assert "wait_after_execution_seconds" not in step["parameters"]
    assert step["handler_id"] == "maa.pipeline.wait_text"
    assert step["wrappers"] == (
        [
            {
                "kind": "wait_after_execution",
                "handler_id": "maa.wrapper.wait_after_execution",
                "parameters": {"seconds": seconds},
            }
        ]
        if seconds
        else []
    )


def test_registered_extension_retains_its_own_parameter_contract():
    manifest = document("custom_extension", wait_after_execution_seconds=1.25)
    version = ScriptVersionRecord(
        script_version_id=uuid4(),
        script_id=uuid4(),
        revision=1,
        schema_version=2,
        manifest_hash=canonical_manifest_hash(manifest),
        manifest=manifest,
        created_at=datetime.now(UTC),
    )
    registry = MaaActionCompilerRegistry(
        (MaaActionDefinition("custom_extension", "custom.handler"),)
    )
    step = MaaRegisteredActionCompiler(registry).compile(version, []).definition["steps"][0]
    assert step["parameters"]["wait_after_execution_seconds"] == 1.25
    assert step["wrappers"] == []
