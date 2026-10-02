"""Validate the entire imported recovery graph before making any version executable."""

from collections.abc import Sequence
from uuid import UUID

from al1s.maa.errors import MaaDomainError
from al1s.maa.recovery import MAX_RECOVERY_DEPTH, _recovery_targets
from al1s.maa.types import ScriptRecord, ScriptType, ScriptVersionRecord


def validate_import_closure(
    scripts: Sequence[ScriptRecord], versions: Sequence[ScriptVersionRecord]
) -> None:
    identities = {script.script_id: script for script in scripts}
    graph = {version.script_id: _recovery_targets(version.manifest) for version in versions}
    for source, targets in graph.items():
        for target in targets:
            dependency = identities.get(target)
            if (
                dependency is None
                or dependency.application_id != identities[source].application_id
                or dependency.script_type is not ScriptType.MODULE_PROCESS
            ):
                raise MaaDomainError(
                    "archive_recovery_missing",
                    "Recovery must be a process script in this archive and application",
                    422,
                )
    heights: dict[UUID, int] = {}

    def depth(source: UUID, path: frozenset[UUID]) -> int:
        if source in path:
            raise MaaDomainError(
                "failure_retry_cycle", "Imported recovery scripts contain a cycle", 422
            )
        if len(path) > MAX_RECOVERY_DEPTH:
            raise MaaDomainError(
                "failure_retry_depth_exceeded", "Imported recovery chain is too deep", 422
            )
        if source not in heights:
            heights[source] = max(
                (1 + depth(target, path | {source}) for target in graph[source]), default=0
            )
        return heights[source]

    for source in graph:
        if depth(source, frozenset()) > MAX_RECOVERY_DEPTH:
            raise MaaDomainError(
                "failure_retry_depth_exceeded", "Imported recovery chain is too deep", 422
            )
