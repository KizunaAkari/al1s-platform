from datetime import UTC, datetime
from uuid import uuid4

import pytest

from al1s.execution.definitions import canonical_manifest_hash
from al1s.maa.compiler import MaaRegisteredActionCompiler
from al1s.maa.types import ScriptVersionRecord
from al1s.maa.validation import validate_script_document


def document(**settings):
    return {
        "version": 2,
        "script_type": "module_process",
        "target": {"application_package": "com.example"},
        "steps": [
            {"action": "wait_image", "template_base64": "data:image/png;base64,YQ==", **settings}
        ],
    }


@pytest.mark.parametrize("count", [None, 1, 3, 1000000])
def test_accepts_optional_positive_integer_without_an_arbitrary_round_limit(count):
    assert validate_script_document(
        document(consecutive_match_count=count), allow_inline_resources=True
    ).valid


@pytest.mark.parametrize("count", [0, -1, 1.5, True, "3", {}, float("inf"), float("nan")])
def test_rejects_invalid_counts_at_the_field_pointer(count):
    result = validate_script_document(
        document(consecutive_match_count=count), allow_inline_resources=True
    )
    assert any(issue.pointer == "/steps/0/consecutive_match_count" for issue in result.issues)


def test_only_image_wait_owns_the_parameter():
    manifest = document(consecutive_match_count=3)
    manifest["steps"][0]["action"] = "wait_click"
    result = validate_script_document(manifest, allow_inline_resources=True)
    assert any(
        issue.code == "unknown_action_field" and issue.pointer == "/steps/0/consecutive_match_count"
        for issue in result.issues
    )


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"consecutive_match_count": None},
        {"consecutive_match_count": 1},
        {"consecutive_match_count": 3},
    ],
)
def test_immutable_version_compiles_stability_only_when_needed(settings):
    manifest = document(**settings, wait_after_execution_seconds=1.25)
    digest = canonical_manifest_hash(manifest)
    version = ScriptVersionRecord(
        script_version_id=uuid4(),
        script_id=uuid4(),
        revision=1,
        schema_version=2,
        manifest_hash=digest,
        manifest=manifest,
        created_at=datetime.now(UTC),
    )
    compiled = MaaRegisteredActionCompiler().compile(version, []).definition["steps"][0]
    assert "consecutive_match_count" not in compiled["parameters"]
    stability = [
        wrapper for wrapper in compiled["wrappers"] if wrapper["kind"] == "wait_image_stability"
    ]
    assert stability == (
        [
            {
                "kind": "wait_image_stability",
                "handler_id": "maa.wrapper.wait_image_stability",
                "parameters": {"consecutive_match_count": 3},
            }
        ]
        if settings.get("consecutive_match_count") == 3
        else []
    )
    assert compiled["wrappers"][-1]["kind"] == "wait_after_execution"
    assert canonical_manifest_hash(manifest) == digest
    assert manifest["steps"][0].get("consecutive_match_count") == settings.get(
        "consecutive_match_count"
    )
