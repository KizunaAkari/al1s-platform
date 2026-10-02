"""Bounded screenshot references from accepted diagnostics; no image body or local paths."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from al1s.execution.failure_details import failure_details


@dataclass(frozen=True)
class TaskCapture:
    artifact_id: UUID
    file_name: str


def task_captures(diagnostic: dict[str, Any] | None) -> tuple[TaskCapture, ...]:
    found: dict[UUID, TaskCapture] = {}
    pending: list[object] = [diagnostic]
    visited = 0
    # A strategy has up to 1002 modules, each with 256 screenshot references.
    # Keep traversal bounded while allowing references from later modules.
    while pending and visited < 4_000_000 and len(found) < 256 * 1002:
        value = pending.pop()
        visited += 1
        if isinstance(value, dict):
            if value.get("artifact_kind") == "screenshot":
                try:
                    identity = UUID(str(value.get("artifact_id")))
                except ValueError:
                    continue
                name = value.get("file_name")
                found[identity] = TaskCapture(
                    identity, name[:255] if isinstance(name, str) else "screenshot.png"
                )
            else:
                pending.extend(reversed(list(value.values())))
        elif isinstance(value, list):
            pending.extend(reversed(value))
    # Earlier failure reports omitted artifact_kind; preserve their download route.
    for detail in failure_details(diagnostic):
        if detail.screenshot_id and detail.screenshot_id not in found:
            found[detail.screenshot_id] = TaskCapture(detail.screenshot_id, "screenshot.png")
    return tuple(found.values())
