"""Extract only bounded step metadata, never captures, paths or raw node logs."""

from contextlib import suppress
from typing import Any, TypeGuard
from uuid import UUID


def _plain_number(value: object) -> TypeGuard[int | float]:
    return type(value) in (int, float)


def conditional_skip_metadata(diagnostic: dict[str, Any] | None) -> list[dict[str, object]]:
    if not diagnostic:
        return []
    modules = diagnostic.get("modules", [])
    if not isinstance(modules, list):
        return []
    found: list[dict[str, object]] = []
    for module_index, module in enumerate(modules):
        if not isinstance(module, dict) or not isinstance(module.get("result"), dict):
            continue
        skips = module["result"].get("conditional_skips", [])
        if not isinstance(skips, list):
            continue
        for skip_index, skip in enumerate(skips):
            if not isinstance(skip, dict):
                continue
            metadata: dict[str, object] = {
                "diagnostic_pointer": (
                    f"/modules/{module_index}/result/conditional_skips/{skip_index}"
                ),
                "module_number": module_index + 1,
            }
            for key in ("module_number", "module_step_number", "trigger_step_number"):
                value = skip.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    metadata["step_number" if key == "trigger_step_number" else key] = value
            for key, allowed in (
                ("mode", {"numeric", "image"}),
                ("operator", {"gt", "lt"}),
                ("scope", {"current", "until_step"}),
            ):
                if isinstance(skip.get(key), str) and skip[key] in allowed:
                    metadata[key] = skip[key]
            for key in ("value", "threshold"):
                value = skip.get(key)
                if _plain_number(value) and -1e100 <= value <= 1e100:
                    metadata[key] = value
            target = skip.get("target_step_number")
            if type(target) is int and target > 0:
                metadata["target_step_number"] = target
            capture = skip.get("capture")
            if isinstance(capture, dict) and isinstance(capture.get("artifact_id"), str):
                with suppress(ValueError):
                    metadata["capture_id"] = str(UUID(capture["artifact_id"]))
            found.append(metadata)
    return found
