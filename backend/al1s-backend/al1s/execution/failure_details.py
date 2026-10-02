"""Bounded, allow-listed projection of runtime diagnostics for operator details."""

from contextlib import suppress
from dataclasses import dataclass
from math import isfinite
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class FailureDetail:
    module_number: int | None
    script_name: str | None
    step_number: int | None
    stage: str | None
    title: str | None
    message: str | None
    screenshot_id: UUID | None
    screenshot_error: str | None
    click_x: float | None
    click_y: float | None
    actual_score: float | None
    configured_threshold: float | None


def _text(value: object) -> str | None:
    return value[:512] if isinstance(value, str) and value else None


def _number(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _finite(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        with suppress(OverflowError):
            return float(value) if isfinite(value) else None
    return None


def _detail(result: dict[str, Any], module: dict[str, Any]) -> FailureDetail:
    diagnosis = result.get("failure_diagnosis")
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    step = result.get("failed_step")
    step = step if isinstance(step, dict) else {}
    screenshot = result.get("failure_screenshot")
    click = diagnosis.get("click")
    click = click if isinstance(click, dict) else {}
    recognition = diagnosis.get("recognition")
    recognition = recognition if isinstance(recognition, dict) else {}
    screenshot_id = None
    if isinstance(screenshot, dict):
        with suppress(ValueError):
            screenshot_id = UUID(str(screenshot.get("artifact_id")))
    return FailureDetail(
        _number(module.get("module_index")),
        _text(module.get("script_name")) or _text(module.get("definition_key")),
        _number(step.get("number")),
        _text(diagnosis.get("stage")),
        _text(diagnosis.get("title")),
        _text(diagnosis.get("message")) or _text(result.get("error")),
        screenshot_id,
        _text(result.get("failure_screenshot_error")),
        _finite(click.get("x")),
        _finite(click.get("y")),
        _finite(recognition.get("actual_score")),
        _finite(recognition.get("configured_threshold")),
    )


def failure_details(diagnostic: dict[str, Any] | None) -> tuple[FailureDetail, ...]:
    if not diagnostic:
        return ()
    modules = diagnostic.get("modules")
    if not isinstance(modules, list):
        detail = _detail(diagnostic, {})
        return (detail,) if any(value is not None for value in vars(detail).values()) else ()
    return tuple(
        _detail(module["result"], module)
        for module in modules[:100]
        if isinstance(module, dict)
        and isinstance(module.get("result"), dict)
        and module["result"].get("success") is not True
    )


def screenshot_ids(diagnostic: dict[str, Any] | None) -> set[UUID]:
    return {item.screenshot_id for item in failure_details(diagnostic) if item.screenshot_id}


def definitely_no_screenshot(diagnostic: dict[str, Any] | None) -> bool:
    details = failure_details(diagnostic)
    return bool(details) and all(
        item.screenshot_id is None and bool(item.screenshot_error) for item in details
    )
