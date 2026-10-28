from datetime import UTC, datetime
from uuid import uuid4

import pytest

from al1s.execution.definitions import canonical_manifest_hash
from al1s.maa.compiler import MaaRegisteredActionCompiler
from al1s.maa.types import ScriptVersionRecord
from al1s.maa.validation import validate_script_document


def document(**rule):
    return {
        "version": 2,
        "script_type": "module_process",
        "target": {"application_package": "com.example"},
        "steps": [{"action": "wait", "seconds": 1}],
        "global_popups": [
            {
                "recognition_mode": "text",
                "text": "重复挑战(追加)",
                "click_mode": "match_center",
                **rule,
            }
        ],
    }


def test_text_rule_does_not_require_template_and_retains_its_region():
    raw = document(search_region={"x": 1, "y": 2, "width": 200, "height": 50})
    assert validate_script_document(raw).valid
    version = ScriptVersionRecord(
        script_version_id=uuid4(),
        script_id=uuid4(),
        revision=1,
        schema_version=2,
        manifest_hash=canonical_manifest_hash(raw),
        manifest=raw,
        created_at=datetime.now(UTC),
    )
    compiled = MaaRegisteredActionCompiler().compile(version, [])
    assert compiled.provider_keys == ("maa", "ocr")
    assert compiled.definition["independent_rules"] == raw["global_popups"]


@pytest.mark.parametrize(
    "rule",
    [
        {"text": "  "},
        {"text": "a" * 201},
        {"recognition_mode": "unknown"},
        {"search_region": {"x": -1, "y": 0, "width": 20, "height": 20}},
    ],
)
def test_rejects_invalid_text_rule(rule):
    assert not validate_script_document(document(**rule)).valid


def test_disabled_text_rule_does_not_require_ocr_provider():
    raw = document(enabled=False)
    version = ScriptVersionRecord(
        script_version_id=uuid4(),
        script_id=uuid4(),
        revision=1,
        schema_version=2,
        manifest_hash=canonical_manifest_hash(raw),
        manifest=raw,
        created_at=datetime.now(UTC),
    )
    assert MaaRegisteredActionCompiler().compile(version, []).provider_keys == ("maa",)
