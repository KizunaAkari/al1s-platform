"""Recognition evidence tied to an immutable formal task snapshot, not current scripts."""

from collections.abc import Iterator
from math import isfinite
from typing import Any

from pydantic import BaseModel

from al1s.maa.quick_test_diagnostics import QuickTestFailureDetail, project_failure_detail


class TaskRecognitionFailureDetail(QuickTestFailureDetail):
    module_number: int


class TaskFailureContext(BaseModel):
    module_number: int
    script_name: str
    step_number: int | None = None
    rule_name: str | None = None
    timeout_seconds: float | None = None
    elapsed_seconds: float | None = None


def _module_keys(manifest: dict[str, Any]) -> list[str]:
    modules = manifest.get("modules")
    if not isinstance(modules, list) or not 1 <= len(modules) <= 1000:
        return []
    positions: dict[int, str] = {}
    for item in modules:
        if not isinstance(item, dict):
            return []
        position, key = item.get("position"), item.get("definition_key")
        if (
            type(position) is not int
            or position < 0
            or position in positions
            or not isinstance(key, str)
        ):
            return []
        positions[position] = key
    return [positions[position] for position in sorted(positions)]


def _failed_modules(
    manifest: dict[str, Any] | None,
    diagnostic: dict[str, Any] | None,
) -> Iterator[tuple[int, str, dict[str, Any], dict[str, Any]]]:
    if not isinstance(manifest, dict) or not isinstance(diagnostic, dict):
        return
    keys = _module_keys(manifest)
    definitions = manifest.get("definitions")
    if not isinstance(definitions, dict):
        return
    reports = diagnostic.get("modules")
    reports = reports[:100] if isinstance(reports, list) else [diagnostic]
    for module in reports:
        if not isinstance(module, dict):
            continue
        result = module.get("result", module)
        index = module.get("module_index")
        if (
            not isinstance(result, dict)
            or result.get("success") is True
            or type(index) is not int
            or not 1 <= index <= len(keys)
            or module.get("definition_key") != keys[index - 1]
        ):
            continue
        definition = definitions.get(keys[index - 1])
        if isinstance(definition, dict):
            yield index, keys[index - 1], definition, result


def project_task_recognition_failures(
    manifest: dict[str, Any] | None, diagnostic: dict[str, Any] | None,
) -> list[TaskRecognitionFailureDetail]:
    projected = []
    for index, key, definition, result in _failed_modules(manifest, diagnostic):
        detail = project_failure_detail(
            {"manifest": {"definitions": {key: definition}}},
            {**result, "definition_key": key},
        )
        if detail is not None:
            projected.append(
                TaskRecognitionFailureDetail(**detail.model_dump(), module_number=index)
            )
    return projected


def _context(
    index: int, definition: dict[str, Any], result: dict[str, Any],
) -> TaskFailureContext | None:
    raw = result.get("execution_failure")
    failed = result.get("failed_step")
    step = None
    if raw is not None and not isinstance(raw, dict):
        return None
    if isinstance(raw, dict):
        step = raw.get("step_index")
    elif isinstance(failed, dict):
        step = failed.get("index")
        if step is None and type(failed.get("number")) is int:
            step = failed["number"] - 1
    steps = definition.get("steps")
    if step is not None and (
        type(step) is not int or not isinstance(steps, list) or not 0 <= step < len(steps)
    ):
        return None
    detail = TaskFailureContext(module_number=index,
                               script_name=str(definition.get("script_name") or "脚本")[:200],
                               step_number=step + 1 if step is not None else None)
    if isinstance(raw, dict):
        if "rule_index" in raw:
            rule = raw["rule_index"]
            rules = definition.get("independent_rules")
            if (type(rule) is not int or step is None or not isinstance(rules, list)
                    or not 0 <= rule < len(rules) or not isinstance(rules[rule], dict)):
                return None
            detail.rule_name = str(rules[rule].get("name") or f"独立规则 {rule + 1}")[:200]
        for key in ("timeout_seconds", "elapsed_seconds"):
            value: Any = raw.get(key)
            if type(value) in {float, int} and isfinite(value) and 0 <= value <= 86_400:
                setattr(detail, key, float(value))
    return detail


def project_task_failure_contexts(
    manifest: dict[str, Any] | None, diagnostic: dict[str, Any] | None,
) -> list[TaskFailureContext]:
    return [detail for index, _key, definition, result in _failed_modules(manifest, diagnostic)
            if (detail := _context(index, definition, result)) is not None]
