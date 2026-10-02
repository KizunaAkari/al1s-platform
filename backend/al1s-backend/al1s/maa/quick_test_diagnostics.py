"""Safe display-only projection from a receipt and its immutable definition."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel


class QuickTestFailureDetail(BaseModel):
    step_number: int
    script_name: str
    rule_name: str | None = None
    stage: Literal["recognition", "click_target", "post_assertion"]
    algorithm: Literal["TemplateMatch", "OCR"]
    best_score: float | None = None
    threshold: float | None = None
    consecutive_misses: int | None = None
    timeout_seconds: float | None = None
    elapsed_seconds: float | None = None


def _number(value: Any, low: float, high: float) -> float | None:
    if type(value) in {int, float} and math.isfinite(value) and low <= value <= high:
        return float(value)
    return None


def project_failure_detail(
    definition: dict[str, Any],
    diagnostic: dict[str, Any],
) -> QuickTestFailureDetail | None:
    raw = diagnostic.get("recognition_failure")
    manifest = definition.get("manifest", {})
    modules = manifest.get("definitions", {})
    key = diagnostic.get("definition_key")
    module = modules.get(key) if isinstance(key, str) and isinstance(modules, dict) else None
    if not isinstance(raw, dict) or not isinstance(module, dict):
        return None
    index = raw.get("step_index")
    steps = module.get("steps", [])
    if type(index) is not int or not isinstance(steps, list) or not 0 <= index < len(steps):
        return None
    stage, algorithm = raw.get("stage"), raw.get("algorithm")
    if stage not in ("recognition", "click_target", "post_assertion"):
        return None
    if algorithm not in ("TemplateMatch", "OCR"):
        return None
    rule_name = None
    if "rule_index" in raw:
        rules = module.get("independent_rules", [])
        rule_index = raw["rule_index"]
        if (
            type(rule_index) is not int
            or not isinstance(rules, list)
            or not 0 <= rule_index < len(rules)
            or not isinstance(rules[rule_index], dict)
        ):
            return None
        rule_name = str(rules[rule_index].get("name") or f"独立规则 {rule_index + 1}")[:200]
    selected = manifest.get("debug_step_number")
    number = selected if type(selected) is int and 1 <= selected <= 1000 else index + 1
    misses = raw.get("consecutive_misses")
    return QuickTestFailureDetail(
        step_number=number,
        script_name=str(module.get("script_name") or "脚本")[:200],
        rule_name=rule_name,
        stage=stage,
        algorithm=algorithm,
        best_score=_number(raw.get("best_score"), -1, 1),
        threshold=_number(raw.get("threshold"), 0, 1),
        consecutive_misses=misses if type(misses) is int and 0 <= misses <= 1_000_000 else None,
        timeout_seconds=_number(raw.get("timeout_seconds"), 0, 86_400),
        elapsed_seconds=_number(raw.get("elapsed_seconds"), 0, 86_400),
    )
