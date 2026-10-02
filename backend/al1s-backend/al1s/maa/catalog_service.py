from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any
from uuid import UUID

from al1s.maa.errors import MaaDomainError
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.types import (
    ApplicationRecord,
    ImportBatchRecord,
    ImportItemRecord,
    ScriptRecord,
    ScriptVersionRecord,
    StrategyModuleRecord,
    StrategyRecord,
    StrategyVersionRecord,
)

MaaUowFactory = Callable[[], MaaUnitOfWork]


class MaaCatalogQueryService:
    """Bounded read model for Maa content-management APIs."""

    def __init__(self, uow_factory: MaaUowFactory) -> None:
        self._uow_factory = uow_factory

    def list_applications(
        self, *, after_id: UUID | None, limit: int
    ) -> list[tuple[ApplicationRecord, int]]:
        with self._uow_factory() as uow:
            applications = uow.applications.list_active(after_id=after_id, limit=limit)
            counts = uow.scripts.count_saved_by_applications(
                [application.application_id for application in applications]
            )
            return [
                (application, counts.get(application.application_id, 0))
                for application in applications
            ]

    def get_application(self, application_id: UUID) -> ApplicationRecord:
        with self._uow_factory() as uow:
            application = uow.applications.get_active(application_id)
            if application is None:
                raise MaaDomainError("application_not_found", "Application was not found", 404)
            return application

    def application_actions(self, application_id: UUID) -> dict[str, str | None]:
        """Project commands for one selected category; mutations recheck in transaction."""
        with self._uow_factory() as uow:
            if uow.applications.get_active(application_id) is None:
                raise MaaDomainError("application_not_found", "Application was not found", 404)
            return {
                "rename": None,
                "delete": None,
            }

    def application_delete_preview(self, application_id: UUID) -> dict[str, Any]:
        with self._uow_factory() as uow:
            if uow.applications.get_active(application_id) is None:
                raise MaaDomainError("application_not_found", "Application was not found", 404)
            script_count, strategy_count, device_count = uow.application_deletions.counts(
                application_id
            )
            blockers = uow.application_deletions.active_tasks(application_id)
            return {
                "script_count": script_count,
                "strategy_count": strategy_count,
                "device_count": device_count,
                "blocking_task_ids": [str(item) for item in blockers[:50]],
                "has_more_blockers": len(blockers) > 50,
            }

    def list_scripts(
        self,
        *,
        application_id: UUID | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[ScriptRecord]:
        with self._uow_factory() as uow:
            if application_id is not None and uow.applications.get_active(application_id) is None:
                raise MaaDomainError("application_not_found", "Application was not found", 404)
            return uow.scripts.list_active(
                application_id=application_id,
                after_id=after_id,
                limit=limit,
            )

    def list_editor_drafts(
        self, *, device_id: UUID, after_id: UUID | None, limit: int
    ) -> list[ScriptRecord]:
        with self._uow_factory() as uow:
            return uow.scripts.list_editor_drafts(
                device_id=device_id, after_id=after_id, limit=limit,
            )

    def list_library_scripts(
        self, *, application_id: UUID, after_id: UUID | None, limit: int,
    ) -> list[ScriptRecord]:
        with self._uow_factory() as uow:
            if uow.applications.get_active(application_id) is None:
                raise MaaDomainError("application_not_found", "Application was not found", 404)
            return uow.scripts.list_saved_for_library(
                application_id=application_id, after_id=after_id, limit=limit,
            )

    def get_script(self, script_id: UUID) -> ScriptRecord:
        with self._uow_factory() as uow:
            script = uow.scripts.get_active(script_id)
            if script is None:
                raise MaaDomainError("script_not_found", "Script was not found", 404)
            return script

    def script_referrers(
        self, script_id: UUID, *, after_id: UUID | None, limit: int
    ) -> list[dict[str, object]]:
        with self._uow_factory() as uow:
            if uow.scripts.get_active(script_id) is None:
                raise MaaDomainError("script_not_found", "Script was not found", 404)
            return uow.scripts.list_referrers(script_id, after_id=after_id, limit=limit)

    def list_script_versions(
        self,
        *,
        script_id: UUID,
        after_revision: int | None,
        limit: int,
        newest_first: bool = False,
    ) -> list[ScriptVersionRecord]:
        with self._uow_factory() as uow:
            if uow.scripts.get_active(script_id) is None:
                raise MaaDomainError("script_not_found", "Script was not found", 404)
            if newest_first:
                return uow.script_versions.list_for_script(
                    script_id,
                    after_revision=after_revision,
                    limit=limit,
                    newest_first=True,
                )
            return uow.script_versions.list_for_script(
                script_id,
                after_revision=after_revision,
                limit=limit,
            )

    def get_script_version(
        self, *, script_id: UUID, script_version_id: UUID
    ) -> ScriptVersionRecord:
        with self._uow_factory() as uow:
            version = uow.script_versions.get(script_version_id)
            if version is None or version.script_id != script_id:
                raise MaaDomainError(
                    "script_version_not_found", "Script version was not found", 404
                )
            return version

    def list_strategies(
        self,
        *,
        application_id: UUID | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[StrategyRecord]:
        with self._uow_factory() as uow:
            if application_id is not None and uow.applications.get_active(application_id) is None:
                raise MaaDomainError("application_not_found", "Application was not found", 404)
            return uow.strategies.list_active(
                application_id=application_id,
                after_id=after_id,
                limit=limit,
            )

    def get_strategy(self, strategy_id: UUID) -> StrategyRecord:
        with self._uow_factory() as uow:
            strategy = uow.strategies.get_active(strategy_id)
            if strategy is None:
                raise MaaDomainError("strategy_not_found", "Strategy was not found", 404)
            return strategy

    def get_strategy_edit_definition(
        self, strategy_id: UUID,
    ) -> tuple[StrategyRecord, dict[str, Any]]:
        """Four bounded reads, including one ID-only batch; no per-module queries."""
        with self._uow_factory() as uow:
            strategy = uow.strategies.get_active(strategy_id)
            if strategy is None:
                raise MaaDomainError("strategy_not_found", "Strategy was not found", 404)
            if strategy.current_version_id is None:
                raise MaaDomainError("strategy_version_not_found", "Published version missing", 409)
            version = uow.strategy_versions.get(strategy.current_version_id)
            if version is None or version.strategy_id != strategy_id:
                raise MaaDomainError("strategy_version_not_found", "Published version missing", 409)
            modules = sorted(uow.strategy_versions.list_modules(version.strategy_version_id),
                             key=lambda item: item.position)
            if (not 2 <= len(modules) <= 1002 or modules[0].module_role.value != "start"
                    or modules[-1].module_role.value != "end"
                    or any(m.module_role.value != "process" for m in modules[1:-1])):
                raise MaaDomainError("invalid_strategy_modules", "Invalid module order", 409)
            owners = uow.script_versions.script_ids_for_versions(
                [m.script_version_id for m in modules]
            )
            if any(m.script_version_id not in owners for m in modules):
                raise MaaDomainError("script_version_not_found", "Referenced version missing", 409)
            return strategy, {
                "start_script_id": owners[modules[0].script_version_id],
                "end_script_id": owners[modules[-1].script_version_id],
                "start_wait_after_ms": modules[0].wait_after_ms,
                "process_modules": [
                    {"script_id": owners[m.script_version_id], "wait_after_ms": m.wait_after_ms}
                    for m in modules[1:-1]
                ],
                "default_parameters": deepcopy(version.manifest.get("default_parameters", {})),
            }

    def list_strategy_versions(
        self, *, strategy_id: UUID, after_revision: int | None, limit: int
    ) -> list[StrategyVersionRecord]:
        with self._uow_factory() as uow:
            if uow.strategies.get_active(strategy_id) is None:
                raise MaaDomainError("strategy_not_found", "Strategy was not found", 404)
            return uow.strategy_versions.list_for_strategy(
                strategy_id,
                after_revision=after_revision,
                limit=limit,
            )

    def get_strategy_version(
        self, *, strategy_id: UUID, strategy_version_id: UUID
    ) -> tuple[StrategyVersionRecord, list[StrategyModuleRecord]]:
        with self._uow_factory() as uow:
            version = uow.strategy_versions.get(strategy_version_id)
            if version is None or version.strategy_id != strategy_id:
                raise MaaDomainError(
                    "strategy_version_not_found", "Strategy version was not found", 404
                )
            return version, uow.strategy_versions.list_modules(strategy_version_id)

    def get_import(self, batch_id: UUID) -> ImportBatchRecord:
        with self._uow_factory() as uow:
            batch = uow.imports.get(batch_id)
            if batch is None:
                raise MaaDomainError("import_batch_not_found", "Import batch was not found", 404)
            return batch

    def list_import_items(
        self,
        *,
        batch_id: UUID,
        after_ordinal: int | None,
        limit: int,
    ) -> list[ImportItemRecord]:
        with self._uow_factory() as uow:
            if uow.imports.get(batch_id) is None:
                raise MaaDomainError("import_batch_not_found", "Import batch was not found", 404)
            return uow.imports.list_items(
                batch_id,
                after_ordinal=after_ordinal,
                limit=limit,
            )
