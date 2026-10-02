"""Display names come from the event's immutable definition, never current scripts."""

import re
from dataclasses import replace
from typing import Any

from al1s.maa.types import QuickTestEventRecord


def project_rule_event(
    event: QuickTestEventRecord, definition: dict[str, Any]
) -> QuickTestEventRecord:
    if event.kind != "log" or event.step_number is not None:
        return event
    match = re.fullmatch(r"maa_rule_(?:started|succeeded|failed):(.+):(\d+)", event.code or "")
    if not match or not 0 <= int(match[2]) < 50:
        return event
    manifest = definition.get("manifest", {})
    modules = manifest.get("definitions", {}) if isinstance(manifest, dict) else {}
    module = modules.get(match[1]) if isinstance(modules, dict) else None
    rules = module.get("independent_rules", []) if isinstance(module, dict) else []
    index = int(match[2])
    if not isinstance(rules, list) or index >= len(rules) or not isinstance(rules[index], dict):
        return event
    name = str(rules[index].get("name") or f"独立规则 {index + 1}")[:200]
    return replace(event, rule_name=name)
