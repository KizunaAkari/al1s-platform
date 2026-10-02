from __future__ import annotations

from copy import deepcopy

from al1s.maa.validation import normalize_legacy_end_script, validate_script_document


def test_normalizes_only_the_approved_legacy_end_shape() -> None:
    legacy = {
        "version": 2,
        "script_type": "module_process",
        "steps": [{"action": "wait", "seconds": 1}, {"action": "cleanup"}],
        "cleanup_on_finish": True,
        "target": {"application_package": "com.example"},
    }
    assert normalize_legacy_end_script(legacy)
    assert legacy == {
        "version": 2,
        "script_type": "module_end",
        "steps": [{"action": "wait", "seconds": 1}],
        "cleanup_on_finish": True,
        "target": {"application_package": "com.example"},
    }
    assert validate_script_document(legacy).valid

    near_matches = [
        {**deepcopy(legacy), "script_type": "module_process", "cleanup_on_finish": False},
        {
            "version": 2,
            "script_type": "module_process",
            "steps": [{"action": "cleanup"}],
            "target": {"application_package": "com.example"},
        },
        {
            "version": 2,
            "script_type": "module_process",
            "steps": [{"action": "cleanup"}, {"action": "wait", "seconds": 1}],
            "cleanup_on_finish": True,
            "target": {"application_package": "com.example"},
        },
    ]
    for candidate in near_matches:
        original = deepcopy(candidate)
        assert not normalize_legacy_end_script(candidate)
        assert candidate == original


def test_rejects_process_lifecycle_actions_unknown_actions_and_code() -> None:
    result = validate_script_document(
        {
            "version": 2,
            "script_type": "module_process",
            "steps": [
                {"action": "launch_app", "package": "com.example"},
                {"action": "custom_unregistered", "python": "print('unsafe')"},
            ],
            "cleanup_on_finish": True,
            "target": {"application_package": "com.example"},
        }
    )
    assert not result.valid
    assert {issue.code for issue in result.issues} == {
        "arbitrary_code_not_allowed",
        "process_script_cleanup_flag",
        "process_script_lifecycle_action",
        "unregistered_action",
    }


def test_validates_start_end_and_skip_boundaries() -> None:
    start = validate_script_document(
        {
            "version": 2,
            "script_type": "module_start",
            "steps": [
                {"action": "start"},
                {"action": "launch_app", "package": "com.example"},
            ],
            "target": {"application_package": "com.example"},
        }
    )
    assert start.valid

    end = validate_script_document(
        {
            "version": 2,
            "script_type": "module_end",
            "steps": [],
            "cleanup_on_finish": True,
            "target": {"application_package": "com.example"},
        }
    )
    assert end.valid

    invalid_skip = validate_script_document(
        {
            "version": 2,
            "script_type": "standard",
            "steps": [
                {
                    "action": "wait",
                    "seconds": 1,
                    "skip_condition": {
                        "enabled": True,
                        "mode": "numeric",
                        "operator": "gt",
                        "value": 0,
                        "skip_to_step_index": 3,
                    },
                }
            ],
            "target": {"application_package": "com.example"},
        }
    )
    assert [issue.code for issue in invalid_skip.issues] == ["invalid_skip_target"]
