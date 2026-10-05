from uuid import uuid4

import pytest


def fixture():
    image = str(uuid4())
    params = {"template_rect": {"x": 2064, "y": 108, "width": 99, "height": 84},
              "threshold": .85}
    manifest = {"modules": [{"position": 0, "definition_key": "entry"}], "definitions": {
        "entry": {"target": {"screen_size": {"width": 2400, "height": 1080}},
                  "steps": [{"action_id": "recognize_execute", "parameters": params}]}}}
    raw = {"module_index": 1, "definition_key": "entry", "failed_step": {"index": 0},
           "recognition_failure": {"step_index": 0, "stage": "recognition"},
           "failure_screenshot": {"artifact_id": image}, "failure_diagnosis": {"recognition": {
               "actual_score": .724205, "configured_threshold": .85,
               "screen_size": {"width": 2400, "height": 1080},
               "best_box": {"x": 2100, "y": 110, "width": 99, "height": 84}}}}
    return manifest, raw, image


def projection(manifest, raw):
    try:
        from al1s.maa import task_failure_geometry
    except ImportError:
        return {}
    return task_failure_geometry.project_task_failure_geometries(manifest, raw)


@pytest.mark.parametrize("nested", [False, True])
def test_frozen_selection_and_screenshot_candidate_are_distinct(nested):
    manifest, raw, image = fixture()
    report = (
        {"modules": [{"module_index": 1, "definition_key": "entry", "result": raw}]}
        if nested else raw
    )
    result = projection(manifest, report)[image]
    assert result.template_rect.x == 2064 and result.match_rect.x == 2100
    assert result.match_score == .724205 and result.match_passed is False
    assert result.search_region is None and result.basis_size.width == 2400


def test_old_receipt_keeps_expected_rect_but_never_invents_actual_candidate():
    manifest, raw, image = fixture()
    raw["failure_diagnosis"] = {}
    result = projection(manifest, raw)[image]
    assert result.template_rect is not None and result.match_rect is None


def test_wrong_snapshot_identity_and_invalid_coordinates_are_rejected():
    manifest, raw, image = fixture()
    raw["definition_key"] = "wrong"
    assert projection(manifest, raw) == {}
    raw["definition_key"] = "entry"
    raw["failure_diagnosis"]["recognition"]["best_box"]["width"] = -1
    assert projection(manifest, raw)[image].match_rect is None


@pytest.mark.parametrize("scope", ["click", "assertion", "rule"])
def test_geometry_uses_the_selected_click_assertion_or_rule_configuration(scope):
    manifest, raw, image = fixture()
    definition = manifest["definitions"]["entry"]
    params = definition["steps"][0]["parameters"]
    selected = {"x": 10, "y": 20, "width": 30, "height": 40}
    if scope == "click":
        params.update(click_template_rect=selected, execution_mode="image_center")
        raw["recognition_failure"]["stage"] = "click_target"
    elif scope == "assertion":
        params["post_assertion"] = {"template_rect": selected}
        raw["recognition_failure"]["stage"] = "post_assertion"
    else:
        definition["independent_rules"] = [{"template_rect": selected}]
        raw["recognition_failure"]["rule_index"] = 0
    assert projection(manifest, raw)[image].template_rect.x == 10
