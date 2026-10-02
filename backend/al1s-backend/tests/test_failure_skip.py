from datetime import UTC, datetime
from uuid import uuid4

import pytest

from al1s.execution.definitions import canonical_manifest_hash
from al1s.maa.compiler import MaaRegisteredActionCompiler
from al1s.maa.types import ScriptVersionRecord
from al1s.maa.validation import validate_script_document


def document(target=None):
    condition = {"enabled": True, "mode": "execution_failure"}
    if target is not None:
        condition["skip_to_step_index"] = target
    return {
        "version": 2,
        "script_type": "module_process",
        "target": {"application_package": "com.example"},
        "steps": [
            {"action": "back", "skip_condition": condition},
            {"action": "home"},
        ],
    }


@pytest.mark.parametrize("mode", ["recognition_failure", "execution_failure"])
def test_failure_skip_needs_no_image_or_numeric_region_and_has_a_new_capability(mode):
    manifest = document()
    manifest["steps"][0]["skip_condition"]["mode"] = mode
    assert validate_script_document(manifest).valid
    version = ScriptVersionRecord(
        script_version_id=uuid4(),
        script_id=uuid4(),
        revision=1,
        schema_version=2,
        manifest_hash=canonical_manifest_hash(manifest),
        manifest=manifest,
        created_at=datetime.now(UTC),
    )
    compiled = MaaRegisteredActionCompiler().compile(version, [], script_name="optional step")
    wrapper = compiled.definition["steps"][0]["wrappers"][0]
    assert wrapper == {
        "kind": "conditional_skip",
        "handler_id": "maa.wrapper.failure_skip",
        "parameters": {"enabled": True, "mode": mode},
    }
    assert "ocr" not in compiled.definition.get("required_providers", [])


@pytest.mark.parametrize("target", [1, 0, 3, True, 1.5])
def test_failure_skip_keeps_forward_only_target_validation(target):
    result = validate_script_document(document(target))
    assert "invalid_skip_target" in {issue.code for issue in result.issues}


def test_disabled_failure_skip_remains_optional():
    manifest = document()
    manifest["steps"][0]["skip_condition"]["enabled"] = False
    assert validate_script_document(manifest).valid
