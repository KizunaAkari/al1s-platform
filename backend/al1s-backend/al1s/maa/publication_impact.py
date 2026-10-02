from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from al1s.execution.definitions import canonical_manifest_hash
from al1s.maa.errors import MaaDomainError
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.strategy_manifest import build_strategy_manifest
from al1s.maa.types import (
    AffectedStrategyPublication,
    ScriptPublicationImpact,
    ScriptRecord,
    ScriptVersionRecord,
    StrategyModuleRecord,
    StrategyRecord,
    StrategyVersionRecord,
)

MAA_SOURCE_MODULE = "maa"


@dataclass(frozen=True, slots=True)
class DerivedStrategyPublication:
    previous: StrategyRecord
    published: StrategyRecord
    version: StrategyVersionRecord


class MaaPublicationImpactCoordinator:
    """Preview and apply script-to-strategy publication propagation."""

    def preview(
        self,
        uow: MaaUnitOfWork,
        script: ScriptRecord,
        candidate: ScriptVersionRecord,
        *,
        lock_strategies: bool,
    ) -> ScriptPublicationImpact:
        strategies = self._affected_strategies(
            uow,
            script.current_version_id,
            for_update=lock_strategies,
        )
        logical_content_ids = [f"script:{script.script_id}"]
        logical_content_ids.extend(f"strategy:{strategy.strategy_id}" for strategy in strategies)
        schedules = uow.schedule_impacts.list_active_future_impacts(
            source_module=MAA_SOURCE_MODULE,
            logical_content_ids=logical_content_ids,
        )
        affected = tuple(
            AffectedStrategyPublication(
                strategy_id=strategy.strategy_id,
                strategy_name=strategy.name,
                current_version_id=cast(UUID, strategy.current_version_id),
                row_version=strategy.row_version,
            )
            for strategy in strategies
        )
        fingerprint = {
            "script_id": str(script.script_id),
            "script_row_version": script.row_version,
            "current_version_id": (
                str(script.current_version_id) if script.current_version_id else None
            ),
            "candidate_version_id": str(candidate.script_version_id),
            "candidate_manifest_hash": candidate.manifest_hash,
            "strategies": [
                {
                    "strategy_id": str(item.strategy_id),
                    "current_version_id": str(item.current_version_id),
                    "row_version": item.row_version,
                }
                for item in affected
            ],
            "schedules": [
                {
                    "task_id": str(item.task_id),
                    "schedule_id": str(item.schedule_id),
                    "logical_content_id": item.logical_content_id,
                    "status": item.schedule_status.value,
                    "future_occurrence_count": item.future_occurrence_count,
                    "first_scheduled_for": (
                        item.first_scheduled_for.isoformat() if item.first_scheduled_for else None
                    ),
                    "last_scheduled_for": (
                        item.last_scheduled_for.isoformat() if item.last_scheduled_for else None
                    ),
                    "task_row_version": item.task_row_version,
                    "schedule_row_version": item.schedule_row_version,
                }
                for item in schedules
            ],
        }
        return ScriptPublicationImpact(
            script_id=script.script_id,
            current_version_id=script.current_version_id,
            candidate_version_id=candidate.script_version_id,
            candidate_manifest_hash=candidate.manifest_hash,
            affected_strategies=affected,
            active_schedules=tuple(schedules),
            impact_hash=canonical_manifest_hash(fingerprint),
        )

    def derive_strategy_versions(
        self,
        uow: MaaUnitOfWork,
        impact: ScriptPublicationImpact,
        candidate: ScriptVersionRecord,
        *,
        now: datetime,
    ) -> tuple[DerivedStrategyPublication, ...]:
        if not impact.affected_strategies:
            return ()
        strategy_ids = [item.strategy_id for item in impact.affected_strategies]
        strategies = {
            item.strategy_id: item
            for item in self._affected_strategies(
                uow,
                impact.current_version_id,
                for_update=True,
            )
        }
        if set(strategies) != set(strategy_ids):
            raise MaaDomainError(
                "publication_impact_changed",
                "Affected strategies changed after publication preview",
                409,
            )
        current_version_ids = [
            cast(UUID, strategies[strategy_id].current_version_id) for strategy_id in strategy_ids
        ]
        current_versions = uow.strategy_versions.find_by_ids(current_version_ids)
        modules_by_version = uow.strategy_versions.list_modules_many(current_version_ids)
        proposals = self._build_proposals(
            strategies,
            current_versions,
            modules_by_version,
            strategy_ids,
            impact.current_version_id,
            candidate.script_version_id,
            now,
        )
        existing = uow.strategy_versions.find_by_hashes(
            [(item.strategy_id, item.manifest_hash) for item, _ in proposals]
        )
        revisions = uow.strategy_versions.next_revisions(
            [
                item.strategy_id
                for item, _ in proposals
                if (item.strategy_id, item.manifest_hash) not in existing
            ]
        )
        new_versions: list[StrategyVersionRecord] = []
        new_modules: list[StrategyModuleRecord] = []
        resolved_versions: dict[UUID, StrategyVersionRecord] = {}
        for proposal, modules in proposals:
            found = existing.get((proposal.strategy_id, proposal.manifest_hash))
            if found is not None:
                resolved_versions[proposal.strategy_id] = found
                continue
            version = StrategyVersionRecord(
                strategy_version_id=proposal.strategy_version_id,
                strategy_id=proposal.strategy_id,
                revision=revisions[proposal.strategy_id],
                schema_version=proposal.schema_version,
                manifest_hash=proposal.manifest_hash,
                manifest=proposal.manifest,
                created_at=proposal.created_at,
            )
            resolved_versions[proposal.strategy_id] = version
            new_versions.append(version)
            new_modules.extend(modules)
        uow.strategy_versions.add_many(new_versions)
        if new_versions:
            uow.flush()
            uow.strategy_versions.add_modules(new_modules)
        updated = uow.strategies.set_current_versions(
            [
                (
                    strategy_id,
                    strategies[strategy_id].row_version,
                    resolved_versions[strategy_id].strategy_version_id,
                )
                for strategy_id in strategy_ids
            ],
            now,
        )
        if set(updated) != set(strategy_ids):
            raise MaaDomainError(
                "publication_impact_changed",
                "An affected strategy changed concurrently",
                409,
            )
        return tuple(
            DerivedStrategyPublication(
                previous=strategies[strategy_id],
                published=updated[strategy_id],
                version=resolved_versions[strategy_id],
            )
            for strategy_id in strategy_ids
        )

    @staticmethod
    def _affected_strategies(
        uow: MaaUnitOfWork,
        script_version_id: UUID | None,
        *,
        for_update: bool,
    ) -> list[StrategyRecord]:
        if script_version_id is None:
            return []
        return uow.strategies.list_current_using_script_version(
            script_version_id,
            for_update=for_update,
        )

    @staticmethod
    def _build_proposals(
        strategies: dict[UUID, StrategyRecord],
        current_versions: dict[UUID, StrategyVersionRecord],
        modules_by_version: dict[UUID, list[StrategyModuleRecord]],
        strategy_ids: list[UUID],
        old_script_version_id: UUID | None,
        new_script_version_id: UUID,
        now: datetime,
    ) -> list[tuple[StrategyVersionRecord, list[StrategyModuleRecord]]]:
        proposals: list[tuple[StrategyVersionRecord, list[StrategyModuleRecord]]] = []
        for strategy_id in strategy_ids:
            strategy = strategies[strategy_id]
            current_id = cast(UUID, strategy.current_version_id)
            current = current_versions.get(current_id)
            modules = modules_by_version.get(current_id)
            if current is None or modules is None:
                raise MaaDomainError(
                    "strategy_closure_missing",
                    "Affected strategy closure is incomplete",
                    409,
                )
            version_id = uuid4()
            derived_modules = [
                StrategyModuleRecord(
                    strategy_version_id=version_id,
                    position=item.position,
                    module_role=item.module_role,
                    script_version_id=(
                        new_script_version_id
                        if item.script_version_id == old_script_version_id
                        else item.script_version_id
                    ),
                    wait_after_ms=item.wait_after_ms,
                )
                for item in modules
            ]
            defaults = current.manifest.get("default_parameters", {})
            if not isinstance(defaults, dict):
                raise MaaDomainError(
                    "strategy_manifest_invalid",
                    "Affected strategy default parameters are invalid",
                    409,
                )
            manifest = build_strategy_manifest(
                strategy.application_id,
                derived_modules,
                default_parameters=cast(dict[str, Any], defaults),
            )
            proposals.append(
                (
                    StrategyVersionRecord(
                        strategy_version_id=version_id,
                        strategy_id=strategy_id,
                        revision=0,
                        schema_version=1,
                        manifest_hash=canonical_manifest_hash(manifest),
                        manifest=manifest,
                        created_at=now,
                    ),
                    derived_modules,
                )
            )
        return proposals
