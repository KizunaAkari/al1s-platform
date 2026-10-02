from __future__ import annotations

import pytest

from al1s.maa.quick_test_diagnostics import project_failure_detail


def data():
    definition = {
        "manifest": {
            "definitions": {
                "entry": {
                    "script_name": "开始脚本",
                    "steps": [{}, {}, {}, {}],
                    "independent_rules": [{"name": "通知"}],
                }
            }
        }
    }
    diagnostic = {
        "definition_key": "entry",
        "recognition_failure": {
            "step_index": 3,
            "rule_index": 0,
            "stage": "click_target",
            "algorithm": "TemplateMatch",
            "best_score": 0.84406,
            "threshold": 0.85,
            "consecutive_misses": 9,
            "timeout_seconds": 30,
            "elapsed_seconds": 32,
            "rule_name": "https://private/?token=secret",
            "raw": "secret",
        },
    }
    return definition, diagnostic


def test_projection_binds_names_to_immutable_module_and_excludes_raw_fields():
    definition, diagnostic = data()
    result = project_failure_detail(definition, diagnostic)
    assert result is not None
    assert result.model_dump() == {
        "step_number": 4,
        "script_name": "开始脚本",
        "rule_name": "通知",
        "stage": "click_target",
        "algorithm": "TemplateMatch",
        "best_score": 0.84406,
        "threshold": 0.85,
        "consecutive_misses": 9,
        "timeout_seconds": 30,
        "elapsed_seconds": 32,
    }
    assert "secret" not in result.model_dump_json()


def test_single_step_preserves_original_selected_number():
    definition, diagnostic = data()
    definition["manifest"]["debug_step_number"] = 8
    diagnostic["recognition_failure"]["step_index"] = 0
    assert project_failure_detail(definition, diagnostic).step_number == 8


@pytest.mark.parametrize(
    "key,value",
    [
        ("step_index", -1),
        ("step_index", True),
        ("step_index", 4),
        ("rule_index", -1),
        ("rule_index", 1),
        ("stage", "private-stage"),
        ("algorithm", "Custom"),
    ],
)
def test_invalid_identity_or_enum_returns_no_details(key, value):
    definition, diagnostic = data()
    diagnostic["recognition_failure"][key] = value
    assert project_failure_detail(definition, diagnostic) is None


def test_old_receipt_and_unknown_module_fall_back():
    definition, diagnostic = data()
    assert project_failure_detail(definition, {}) is None
    diagnostic["definition_key"] = "unknown"
    assert project_failure_detail(definition, diagnostic) is None


def test_bad_numeric_fields_are_omitted_not_zero():
    definition, diagnostic = data()
    diagnostic["recognition_failure"].update(
        best_score=float("nan"),
        threshold="0.85",
        consecutive_misses=True,
        timeout_seconds=float("inf"),
        elapsed_seconds=-1,
    )
    result = project_failure_detail(definition, diagnostic)
    assert result is not None
    for key in (
        "best_score",
        "threshold",
        "consecutive_misses",
        "timeout_seconds",
        "elapsed_seconds",
    ):
        assert getattr(result, key) is None
