import pytest

from al1s.maa.validation import validate_script_document


def validate(rule):
    return validate_script_document(
        {
            "version": 2,
            "script_type": "module_process",
            "target": {"application_package": "com.example"},
            "steps": [{"action": "wait", "seconds": 1}] * 3,
            "global_popups": [
                {
                    "template_base64": "data:image/png;base64,AQ==",
                    "click_mode": "match_center",
                    **rule,
                }
            ],
        },
        allow_inline_resources=True,
    )


@pytest.mark.parametrize(
    "rule",
    [
        {"from_step_index": 2},
        {"from_step_index": 2, "through_step_index": 3},
        {"step_indexes": [1, 3]},
        {"timeout_seconds": 14400},
    ],
)
def test_valid_independent_rule_ranges(rule):
    assert validate(rule).valid


@pytest.mark.parametrize(
    "rule",
    [
        {"from_step_index": 0},
        {"from_step_index": 3, "through_step_index": 2},
        {"step_indexes": []},
        {"step_indexes": [1], "from_step_index": 1},
        {"click_mode": "image"},
        {"timeout_seconds": 14401},
    ],
)
def test_invalid_rule_cannot_reach_execution(rule):
    assert not validate(rule).valid
