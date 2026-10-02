"""Build a non-publishable, immutable single-step test from a candidate definition."""

from copy import deepcopy
from dataclasses import replace
from typing import Any

from al1s.execution.definitions import canonical_manifest_hash
from al1s.execution.scheduling_types import ResolvedExecutionDefinition
from al1s.maa.errors import MaaDomainError


def select_step(
    definition: ResolvedExecutionDefinition, step_number: int
) -> ResolvedExecutionDefinition:
    manifest = deepcopy(definition.manifest)
    root = manifest["definitions"][manifest["entry_definition_key"]]
    steps = root["steps"]
    if type(step_number) is not int or not 1 <= step_number <= len(steps):
        raise MaaDomainError(
            "invalid_debug_step", "Selected step does not exist in this candidate", 422
        )
    selected = steps[step_number - 1]
    selected["step_index"] = 1
    for wrapper in selected.get("wrappers", []):
        if wrapper.get("kind") == "conditional_skip":
            wrapper["parameters"].pop("skip_to_step_index", None)
    root["steps"] = [selected]
    root["cleanup_on_finish"] = False
    root["independent_rules"] = [
        _local_rule(rule)
        for rule in root.get("independent_rules", [])
        if _applies(rule, step_number, len(steps))
    ]
    manifest["debug_step_number"] = step_number
    return replace(definition, manifest=manifest, manifest_hash=canonical_manifest_hash(manifest))


def _applies(rule: dict[str, Any], step: int, count: int) -> bool:
    if "step_indexes" in rule:
        return step in rule["step_indexes"]
    return bool(rule.get("from_step_index", 1) <= step <= rule.get("through_step_index", count))


def _local_rule(rule: dict[str, Any]) -> dict[str, Any]:
    local = deepcopy(rule)
    local.pop("from_step_index", None)
    local.pop("through_step_index", None)
    local["step_indexes"] = [1]
    return local
