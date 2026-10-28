from copy import deepcopy

import pytest

from al1s.maa.task_failure_diagnostics import project_task_recognition_failures
from tests.test_quick_test_diagnostics import data


def contexts(manifest, diagnostic):
    from al1s.maa import task_failure_diagnostics
    return getattr(task_failure_diagnostics, "project_task_failure_contexts", lambda *_: [])(
        manifest, diagnostic
    )


@pytest.mark.parametrize("nested", [False, True])
def test_guard_context_does_not_require_matching_scores(nested):
    manifest, raw = fixture()
    raw.pop("recognition_failure")
    raw["execution_failure"] = {"step_index": 3, "rule_index": 0,
                                "timeout_seconds": 20, "elapsed_seconds": 23.892}
    report = (
        {"modules": [{"module_index": 1, "definition_key": "entry", "result": raw}]}
        if nested else raw
    )
    result = contexts(manifest, report)
    assert len(result) == 1
    assert result[0].script_name == "开始脚本" and result[0].step_number == 4
    assert result[0].rule_name == "通知"
    assert result[0].timeout_seconds == 20 and result[0].elapsed_seconds == 23.892


def test_known_module_without_step_still_displays_immutable_script():
    manifest, raw = fixture()
    raw.pop("recognition_failure")
    result = contexts(manifest, raw)
    assert len(result) == 1 and result[0].script_name == "开始脚本"
    assert result[0].step_number is None


def test_invalid_context_is_not_assigned_to_a_valid_step_or_rule():
    manifest, raw = fixture()
    raw.pop("recognition_failure")
    for field, value in [("step_index", True), ("step_index", 1000), ("rule_index", True)]:
        raw["execution_failure"] = {"step_index": 3, field: value}
        assert contexts(manifest, raw) == []


def fixture():
    definition, diagnostic = data()
    manifest = definition["manifest"]
    manifest["modules"] = [{"position": 0, "definition_key": "entry"}]
    return manifest, {**diagnostic, "module_index": 1}


@pytest.mark.parametrize("nested", [False, True])
def test_task_failure_reuses_debug_projection_for_flat_and_module_reports(nested):
    manifest, result = fixture()
    raw = (
        {
            "modules": [
                {
                    "module_index": 1,
                    "definition_key": "entry",
                    "script_name": "untrusted",
                    "result": result,
                }
            ]
        }
        if nested
        else result
    )
    observed = project_task_recognition_failures(manifest, raw)
    assert len(observed) == 1
    assert observed[0].module_number == 1
    assert observed[0].script_name == "开始脚本"
    assert observed[0].rule_name == "通知"
    assert observed[0].step_number == 4
    assert observed[0].best_score == 0.84406
    assert observed[0].threshold == 0.85
    assert observed[0].consecutive_misses == 9
    assert observed[0].timeout_seconds == 30
    assert "secret" not in str(observed)


def test_module_order_follows_immutable_positions_not_json_array_order():
    manifest, result = fixture()
    manifest["modules"].insert(0, {"position": 10, "definition_key": "other"})
    manifest["definitions"]["other"] = deepcopy(manifest["definitions"]["entry"])
    assert project_task_recognition_failures(manifest, result)[0].script_name == "开始脚本"


@pytest.mark.parametrize(
    "field,value",
    [("module_index", True), ("module_index", 0), ("module_index", 3), ("definition_key", "other")],
)
def test_invalid_or_foreign_module_does_not_project_diagnostics(field, value):
    manifest, result = fixture()
    result[field] = value
    assert project_task_recognition_failures(manifest, result) == []


def test_missing_successful_and_unsupported_snapshot_results_have_no_projection():
    manifest, result = fixture()
    assert project_task_recognition_failures(manifest, None) == []
    assert project_task_recognition_failures({}, result) == []
    assert (
        project_task_recognition_failures(
            manifest,
            {
                "modules": [
                    {
                        "module_index": 1,
                        "definition_key": "entry",
                        "result": {**result, "success": True},
                    }
                ]
            },
        )
        == []
    )
    result["recognition_failure"]["step_index"] = 99
    assert project_task_recognition_failures(manifest, result) == []
