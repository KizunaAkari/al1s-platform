"""Stable script-archive wire fields and canonical digest encoding."""

import json
from typing import Any

ARCHIVE_SCHEMA = "al1s-script-archive/v1"
PORTABLE_ARCHIVE_SCHEMA = "al1s-script-archive/v2"
STRATEGY_ARCHIVE_SCHEMA = "al1s-script-archive/v3"


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def logical_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": manifest["schema"],
        "scope": manifest["source"]["scope"],
        "categories_sha256": manifest["categories_sha256"],
        "report_sha256": manifest["report_sha256"],
        "statistics": manifest["statistics"],
        "scripts": manifest["scripts"],
        "resources": manifest["resources"],
        **(
            {"strategy": manifest["strategy"]}
            if manifest["schema"] == STRATEGY_ARCHIVE_SCHEMA
            else {}
        ),
    }
