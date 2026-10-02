from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from al1s.maa.errors import MaaDomainError
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.types import (
    ScriptBlobReference,
    ScriptStatus,
    ScriptType,
    ScriptVersionRecord,
)

MAX_RECOVERY_DEPTH = 20


@dataclass(frozen=True, slots=True)
class RecoveryClosure:
    versions_by_script: dict[UUID, ScriptVersionRecord]
    references_by_version: dict[UUID, list[ScriptBlobReference]]

    @property
    def definition_keys(self) -> dict[UUID, str]:
        return {
            script_id: f"recovery:{version.script_version_id}"
            for script_id, version in self.versions_by_script.items()
        }


class MaaRecoveryClosureResolver:
    """Resolve process-script recovery references in bounded breadth-first batches."""

    def resolve(
        self,
        uow: MaaUnitOfWork,
        roots: dict[UUID, ScriptVersionRecord],
        *,
        application_id: UUID,
        include_references: bool = True,
    ) -> RecoveryClosure:
        versions = dict(roots)
        manifests = {script_id: version.manifest for script_id, version in roots.items()}
        frontier = self._next_frontier(manifests, set(versions))
        depth = 0
        while frontier:
            depth += 1
            if depth > MAX_RECOVERY_DEPTH:
                raise MaaDomainError(
                    "failure_retry_depth_exceeded",
                    "Failure retry process chain is too deep",
                    409,
                )
            loaded = self._load_frontier(uow, frontier, application_id=application_id)
            versions.update(loaded)
            manifests.update({script_id: version.manifest for script_id, version in loaded.items()})
            frontier = self._next_frontier(manifests, set(versions))
        self._reject_cycles(manifests)
        references = (
            uow.script_versions.list_blob_references_many(
                [version.script_version_id for version in versions.values()]
            )
            if include_references
            else {}
        )
        return RecoveryClosure(versions, references)

    @staticmethod
    def _load_frontier(
        uow: MaaUnitOfWork,
        script_ids: set[UUID],
        *,
        application_id: UUID,
    ) -> dict[UUID, ScriptVersionRecord]:
        scripts = uow.scripts.find_active_by_ids(tuple(script_ids))
        missing = script_ids - set(scripts)
        if missing:
            raise MaaDomainError(
                "failure_retry_process_missing",
                "Failure retry process script does not exist",
                409,
                context={"script_ids": sorted(str(item) for item in missing)},
            )
        invalid = [
            item
            for item in scripts.values()
            if item.application_id != application_id
            or item.script_type is not ScriptType.MODULE_PROCESS
            or item.status is not ScriptStatus.ACTIVE
            or item.current_version_id is None
        ]
        if invalid:
            raise MaaDomainError(
                "failure_retry_process_unavailable",
                "Failure retry must reference a published process script in the same application",
                409,
                context={"script_ids": sorted(str(item.script_id) for item in invalid)},
            )
        version_ids = [item.current_version_id for item in scripts.values()]
        published = uow.script_versions.find_by_ids(
            [item for item in version_ids if item is not None]
        )
        if len(published) != len(scripts):
            raise MaaDomainError(
                "failure_retry_version_missing",
                "Failure retry process version is unavailable",
                409,
            )
        return {
            script_id: published[script.current_version_id]
            for script_id, script in scripts.items()
            if script.current_version_id is not None
        }

    @staticmethod
    def _next_frontier(manifests: dict[UUID, dict[str, Any]], loaded: set[UUID]) -> set[UUID]:
        referenced = {
            target for manifest in manifests.values() for target in _recovery_targets(manifest)
        }
        return referenced - loaded

    @staticmethod
    def _reject_cycles(manifests: dict[UUID, dict[str, Any]]) -> None:
        graph = {
            script_id: tuple(_recovery_targets(manifest))
            for script_id, manifest in manifests.items()
        }
        visited: set[UUID] = set()
        active: set[UUID] = set()

        def visit(script_id: UUID) -> None:
            if script_id in active:
                raise MaaDomainError(
                    "failure_retry_cycle",
                    "Failure retry process scripts contain a cycle",
                    409,
                )
            if script_id in visited:
                return
            active.add(script_id)
            for target in graph.get(script_id, ()):
                visit(target)
            active.remove(script_id)
            visited.add(script_id)

        for script_id in graph:
            visit(script_id)


def _recovery_targets(manifest: dict[str, Any]) -> tuple[UUID, ...]:
    targets: list[UUID] = []
    steps = manifest.get("steps", [])
    if not isinstance(steps, list):
        return ()
    for step in steps:
        if not isinstance(step, dict):
            continue
        retry = step.get("failure_retry")
        if not isinstance(retry, dict) or retry.get("enabled") is not True:
            continue
        try:
            targets.append(UUID(str(retry.get("process_script_id"))))
        except (TypeError, ValueError, AttributeError):
            continue
    return tuple(targets)
