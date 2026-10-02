import json
from pathlib import Path

from al1s.maa.validation import BASE_ACTIONS, validate_script_document

EXAMPLES = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "maa-dsl-v2.examples.json").read_text(
        encoding="utf-8"
    )
)


def test_shared_v2_document_is_valid():
    assert set(EXAMPLES["core_actions"]) == BASE_ACTIONS
    assert validate_script_document(EXAMPLES["valid"]).issues == ()
    # The shared compiler example contains inline image bytes after resource resolution.
    assert validate_script_document(
        EXAMPLES["valid_complex"], allow_inline_resources=True
    ).issues == ()


def test_shared_malformed_action_is_rejected():
    assert "unregistered_action" in {
        issue.code for issue in validate_script_document(EXAMPLES["invalid"]).issues
    }


def test_shared_typed_fields_are_rejected_when_wrong():
    assert "invalid_integer_parameter" in {
        issue.code for issue in validate_script_document(EXAMPLES["invalid_typed"]).issues
    }
    assert "invalid_numeric_parameter" in {
        issue.code for issue in validate_script_document(EXAMPLES["invalid_complex"]).issues
    }
    assert "invalid_numeric_parameter" in {
        issue.code for issue in validate_script_document(EXAMPLES["invalid_branch"]).issues
    }
