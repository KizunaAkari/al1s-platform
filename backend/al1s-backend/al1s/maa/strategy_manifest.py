from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

from al1s.maa.types import StrategyModuleRecord


def build_strategy_manifest(
    application_id: UUID,
    modules: list[StrategyModuleRecord] | tuple[StrategyModuleRecord, ...],
    *,
    default_parameters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "application_id": str(application_id),
        "modules": [
            {
                "position": item.position,
                "role": item.module_role.value,
                "script_version_id": str(item.script_version_id),
                "wait_after_ms": item.wait_after_ms,
            }
            for item in modules
        ],
        "default_parameters": copy.deepcopy(default_parameters),
    }
