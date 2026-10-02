from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from al1s.execution.definitions import canonical_manifest_hash
from al1s.execution.errors import ConflictError
from al1s.maa.compiler import MaaRegisteredActionCompiler
from al1s.maa.types import ScriptBlobReference, ScriptVersionRecord


def test_screenshot_compiles_only_to_registered_handler() -> None:
    manifest = {
        "version": 2,
        "script_type": "module_process",
        "target": {"application_package": "com.example.game"},
        "steps": [{"action": "screenshot", "timeout_seconds": 10}],
    }
    version = ScriptVersionRecord(
        script_version_id=uuid4(),
        script_id=uuid4(),
        revision=1,
        schema_version=2,
        manifest_hash=canonical_manifest_hash(manifest),
        manifest=manifest,
        created_at=datetime.now(UTC),
    )
    compiled = MaaRegisteredActionCompiler().compile(
        version, [], script_name="capture", recovery_keys={}
    )
    assert compiled.definition["steps"][0]["handler_id"] == "maa.registered.screenshot"


def test_compiles_resources_wrappers_and_recovery_into_registered_actions() -> None:
    blob_id = uuid4()
    script_id = uuid4()
    version_id = uuid4()
    recovery_script_id = uuid4()
    manifest = {
        "version": 2,
        "script_type": "standard",
        "target": {"application_package": "com.example.game"},
        "steps": [
            {
                "action": "wait_click",
                "template_base64": {
                    "$blob": str(blob_id),
                    "media_type": "image/png",
                    "sha256": "a" * 64,
                    "size_bytes": 10,
                },
                "skip_condition": {
                    "enabled": True,
                    "mode": "numeric",
                    "operator": "gt",
                    "value": 0,
                    "region": {"x": 1, "y": 2, "width": 3, "height": 4},
                },
                "failure_retry": {
                    "enabled": True,
                    "process_script_id": str(recovery_script_id),
                    "max_retries": 2,
                },
            }
        ],
    }
    version = ScriptVersionRecord(
        script_version_id=version_id,
        script_id=script_id,
        revision=1,
        schema_version=2,
        manifest_hash=canonical_manifest_hash(manifest),
        manifest=manifest,
        created_at=datetime.now(UTC),
    )
    reference = ScriptBlobReference(
        script_version_id=version_id,
        blob_id=blob_id,
        json_pointer="/steps/0/template_base64",
        resource_role="template",
        ordinal=0,
    )

    compiled = MaaRegisteredActionCompiler().compile(
        version,
        [reference],
        script_name="课程表脚本",
        recovery_keys={recovery_script_id: "recovery:version-2"},
    )

    assert compiled.definition["script_name"] == "课程表脚本"
    step = compiled.definition["steps"][0]
    assert step["handler_id"] == "maa.pipeline.wait_click"
    assert step["parameters"]["template_base64"] == {
        "$resource": f"maa/{blob_id}/template",
        "blob_id": str(blob_id),
        "media_type": "image/png",
        "sha256": "a" * 64,
        "size_bytes": 10,
    }
    assert [item["kind"] for item in step["wrappers"]] == [
        "conditional_skip",
        "failure_retry",
    ]
    assert step["wrappers"][1]["parameters"] == {
        "max_retries": 2,
        "recovery_definition_key": "recovery:version-2",
    }
    assert compiled.provider_keys == ("maa", "ocr")


def test_compiler_rejects_a_handler_missing_from_the_deployed_registry() -> None:
    manifest = {
        "version": 2,
        "script_type": "standard",
        "target": {"application_package": "com.example.game"},
        "steps": [{"action": "not_deployed"}],
    }
    version = ScriptVersionRecord(
        script_version_id=uuid4(),
        script_id=uuid4(),
        revision=1,
        schema_version=2,
        manifest_hash=canonical_manifest_hash(manifest),
        manifest=manifest,
        created_at=datetime.now(UTC),
    )

    with pytest.raises(ConflictError) as captured:
        MaaRegisteredActionCompiler().compile(version, [])

    assert captured.value.code == "maa_action_handler_unavailable"


def test_ocr_assertion_compiles_capability_and_keeps_retry_parameters():
    manifest = {
        "version": 2,
        "script_type": "module_process",
        "target": {"application_package": "com.example"},
        "steps": [
            {
                "action": "wait",
                "seconds": 1,
                "post_assertion": {
                    "enabled": True,
                    "recognition_mode": "text",
                    "text": "开始[游戏]+",
                    "max_retries": 2,
                    "timeout_seconds": 20,
                    "poll_interval_seconds": 1,
                },
            }
        ],
    }
    version = ScriptVersionRecord(
        script_version_id=uuid4(),
        script_id=uuid4(),
        revision=1,
        schema_version=2,
        manifest_hash=canonical_manifest_hash(manifest),
        manifest=manifest,
        created_at=datetime.now(UTC),
    )
    result = MaaRegisteredActionCompiler().compile(version, [], script_name="ocr", recovery_keys={})
    wrapper = result.definition["steps"][0]["wrappers"][0]
    assert wrapper["kind"] == "post_assertion"
    assert wrapper["handler_id"] == "maa.wrapper.post_assertion_text"
    assert wrapper["parameters"]["text"] == "开始[游戏]+"
    assert wrapper["parameters"]["max_retries"] == 2
    assert "ocr" in result.provider_keys


def test_region_preview_is_retained_as_blob_reference_but_not_action_parameter():
    from al1s.maa.validation import declared_blob_resources, validate_script_document

    blob_id = uuid4()
    manifest = {
        "version": 2,
        "script_type": "module_process",
        "target": {"application_package": "com.example"},
        "steps": [
            {
                "action": "wait",
                "seconds": 1,
                "region_previews": {
                    "assertion_ocr_base64": {
                        "$blob": str(blob_id),
                        "sha256": "a" * 64,
                        "media_type": "image/png",
                        "size_bytes": 20,
                    }
                },
            }
        ],
    }
    assert validate_script_document(manifest).valid
    resources = declared_blob_resources(manifest)
    assert len(resources) == 1
    assert resources[0].json_pointer == "/steps/0/region_previews/assertion_ocr_base64"
    version = ScriptVersionRecord(
        script_version_id=uuid4(),
        script_id=uuid4(),
        revision=1,
        schema_version=2,
        manifest_hash=canonical_manifest_hash(manifest),
        manifest=manifest,
        created_at=datetime.now(UTC),
    )
    compiled = MaaRegisteredActionCompiler().compile(
        version, [], script_name="preview", recovery_keys={}
    )
    assert compiled.definition["steps"][0]["parameters"] == {"seconds": 1}
