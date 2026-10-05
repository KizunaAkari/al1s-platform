"""Overlay geometry from frozen configuration and the accepted failure PNG review."""
from math import isfinite
from typing import Any
from uuid import UUID

from al1s.execution.recognition_geometry import FailureRecognitionGeometry, ImageSize, PixelRect
from al1s.maa.task_failure_diagnostics import _failed_modules


def _value(value: Any, low: float = 0, high: float = 32768) -> float | None:
    if type(value) in {int, float} and low <= value <= high and isfinite(value):
        return float(value)
    return None


def _rect(value: Any) -> PixelRect | None:
    if not isinstance(value, dict):
        return None
    numbers = [_value(value.get(key)) for key in ("x", "y", "width", "height")]
    if any(number is None for number in numbers) or not numbers[2] or not numbers[3]:
        return None
    return PixelRect(*(float(number) for number in numbers if number is not None))


def _size(value: Any) -> ImageSize | None:
    if isinstance(value, dict) and all(
        type(value.get(key)) is int and 0 < value[key] <= 32768 for key in ("width", "height")
    ):
        return ImageSize(value["width"], value["height"])
    return None


def _parameters(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return value["parameters"] if isinstance(value.get("parameters"), dict) else value


def _configuration(definition: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
    raw = result.get("recognition_failure")
    raw = raw if isinstance(raw, dict) else {}
    failed = result.get("failed_step")
    failed = failed if isinstance(failed, dict) else {}
    index = raw.get("step_index", failed.get("index"))
    steps = definition.get("steps")
    if type(index) is not int or not isinstance(steps, list) or not 0 <= index < len(steps):
        return None
    config = _parameters(steps[index])
    if "rule_index" in raw:
        rules, rule = definition.get("independent_rules"), raw["rule_index"]
        if type(rule) is not int or not isinstance(rules, list) or not 0 <= rule < len(rules):
            return None
        config = _parameters(rules[rule])
    if raw.get("stage") == "post_assertion" or result.get("error_type") == "PostAssertionFailed":
        config = _parameters(config.get("post_assertion"))
    elif raw.get("stage") == "click_target" and config.get("click_template_rect") is not None:
        region = (
            None if config.get("execution_mode") == "image_center"
            else config.get("click_search_region")
        )
        config = {"template_rect": config.get("click_template_rect"),
                  "search_region": region}
    return config


def project_task_failure_geometries(
    manifest: dict[str, Any] | None, diagnostic: dict[str, Any] | None,
) -> dict[str, FailureRecognitionGeometry]:
    result_map = {}
    for _index, _key, definition, result in _failed_modules(manifest, diagnostic):
        config = _configuration(definition, result)
        screenshot = result.get("failure_screenshot")
        if config is None or not isinstance(screenshot, dict):
            continue
        try:
            image = str(UUID(str(screenshot.get("artifact_id"))))
        except ValueError:
            continue
        target = _parameters(definition.get("target"))
        diagnosis = result.get("failure_diagnosis")
        evidence = diagnosis.get("recognition") if isinstance(diagnosis, dict) else None
        evidence = evidence if isinstance(evidence, dict) else {}
        score = _value(evidence.get("actual_score"), -1, 1)
        threshold = _value(evidence.get("configured_threshold"), 0, 1)
        result_map[image] = FailureRecognitionGeometry(
            template_rect=_rect(config.get("template_rect")),
            search_region=_rect(config.get("search_region")),
            basis_size=_size(target.get("screen_size")),
            match_rect=_rect(evidence.get("best_box")),
            match_size=_size(evidence.get("screen_size")),
            match_score=score, match_threshold=threshold,
            match_passed=(
                score >= threshold if score is not None and threshold is not None else None
            ),
        )
    return result_map
