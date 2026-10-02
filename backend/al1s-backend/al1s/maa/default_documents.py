"""Canonical documents created for new editor categories."""

from typing import Any

from al1s.maa.types import ScriptType


def default_end_manifest(package_name: str) -> dict[str, Any]:
    return {
        "version": 2,
        "script_type": ScriptType.MODULE_END.value,
        "steps": [{"action": "cleanup"}],
        "cleanup_on_finish": True,
        "target": {"application_package": package_name},
    }
