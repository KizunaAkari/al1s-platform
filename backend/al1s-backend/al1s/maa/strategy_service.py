from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from al1s.execution.definitions import canonical_manifest_hash
from al1s.kernel.types import NewAuditEntry, NewOutboxEvent
from al1s.maa.errors import MaaDomainError
from al1s.maa.naming import normalize_asset_name
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.strategy_manifest import build_strategy_manifest
from al1s.maa.types import (
    ProcessModuleInput,
    ScriptRecord,
    ScriptStatus,
    ScriptType,
    StrategyModuleRecord,
    StrategyModuleRole,
    StrategyPublicationImpact,
    StrategyRecord,
    StrategyStatus,
    StrategyVersionRecord,
)

MaaUowFactory = Callable[[], MaaUnitOfWork]
MAX_PROCESS_MODULES = 1_000
MAX_WAIT_AFTER_MS = 14_400_000


@dataclass(frozen=True, slots=True)
class StrategySaveResult:
    strategy: StrategyRecord
    version: StrategyVersionRecord
    modules: tuple[StrategyModuleRecord, ...]
    reused_version: bool


class MaaStrategyService:
    def __init__(
        self,
        uow_factory: MaaUowFactory,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now or (lambda: datetime.now(UTC))

    def create_strategy(
        self,
        *,
        application_id: UUID,
        name: str,
        start_script_id: UUID,
        process_modules: Sequence[ProcessModuleInput],
        end_script_id: UUID,
        start_wait_after_ms: int = 0,
        default_parameters: dict[str, Any] | None = None,
        correlation_id: UUID,
    ) -> StrategySaveResult:
        now = self._now()
        try:
            with self._uow_factory() as uow:
                result = self.create_strategy_in_uow(
                    uow,
                    application_id=application_id,
                    name=name,
                    start_script_id=start_script_id,
                    process_modules=process_modules,
                    end_script_id=end_script_id,
                    start_wait_after_ms=start_wait_after_ms,
                    default_parameters=default_parameters or {},
                    correlation_id=correlation_id,
                    now=now,
                )
                uow.commit()
                return result
        except IntegrityError as exc:
            raise MaaDomainError(
                "strategy_name_conflict",
                "An active strategy with the same application and name already exists",
                409,
            ) from exc

    def create_strategy_in_uow(
        self,
        uow: MaaUnitOfWork,
        *,
        application_id: UUID,
        name: str,
        start_script_id: UUID,
        process_modules: Sequence[ProcessModuleInput],
        end_script_id: UUID,
        start_wait_after_ms: int,
        default_parameters: dict[str, Any],
        correlation_id: UUID,
        now: datetime,
    ) -> StrategySaveResult:
        if uow.applications.get_active(application_id) is None:
            raise MaaDomainError("application_not_found", "Application was not found", 404)
        strategy = StrategyRecord(
            strategy_id=uuid4(),
            application_id=application_id,
            name=name.strip(),
            normalized_name=normalize_asset_name(name, asset_kind="strategy"),
            status=StrategyStatus.ACTIVE,
            current_version_id=None,
            created_at=now,
            updated_at=now,
            deleted_at=None,
            row_version=1,
        )
        uow.strategies.add(strategy)
        uow.flush()
        version, modules = self._build_version(
            uow,
            strategy=strategy,
            start_script_id=start_script_id,
            process_modules=process_modules,
            end_script_id=end_script_id,
            start_wait_after_ms=start_wait_after_ms,
            default_parameters=default_parameters,
            now=now,
        )
        uow.strategy_versions.add(version)
        uow.flush()
        uow.strategy_versions.add_modules(modules)
        published = uow.strategies.set_current_version(
            strategy.strategy_id,
            strategy.row_version,
            version.strategy_version_id,
            now,
        )
        if published is None:
            raise MaaDomainError(
                "strategy_version_conflict",
                "Strategy changed concurrently",
                409,
            )
        self._record_published(
            uow,
            published,
            version,
            previous_version_id=None,
            correlation_id=correlation_id,
            occurred_at=now,
        )
        return StrategySaveResult(published, version, tuple(modules), False)

    def update_strategy(
        self,
        *,
        strategy_id: UUID,
        expected_strategy_version: int,
        start_script_id: UUID,
        process_modules: Sequence[ProcessModuleInput],
        end_script_id: UUID,
        start_wait_after_ms: int = 0,
        default_parameters: dict[str, Any] | None = None,
        correlation_id: UUID,
        expected_impact_hash: str | None = None,
    ) -> StrategySaveResult:
        now = self._now()
        with self._uow_factory() as uow:
            result = self.update_strategy_in_uow(
                uow,
                strategy_id=strategy_id,
                expected_strategy_version=expected_strategy_version,
                start_script_id=start_script_id,
                process_modules=process_modules,
                end_script_id=end_script_id,
                start_wait_after_ms=start_wait_after_ms,
                default_parameters=default_parameters or {},
                expected_impact_hash=expected_impact_hash,
                correlation_id=correlation_id,
                now=now,
            )
            uow.commit()
            return result

    def preview_update(
        self,
        *,
        strategy_id: UUID,
        expected_strategy_version: int,
        start_script_id: UUID,
        process_modules: Sequence[ProcessModuleInput],
        end_script_id: UUID,
        start_wait_after_ms: int = 0,
        default_parameters: dict[str, Any] | None = None,
    ) -> StrategyPublicationImpact:
        with self._uow_factory() as uow:
            strategy = self._require_strategy(
                uow, strategy_id, expected_strategy_version, for_update=False
            )
            proposed, _ = self._build_version(
                uow,
                strategy=strategy,
                start_script_id=start_script_id,
                process_modules=process_modules,
                end_script_id=end_script_id,
                start_wait_after_ms=start_wait_after_ms,
                default_parameters=default_parameters or {},
                now=self._now(),
            )
            return self._preview_impact(uow, strategy, proposed)

    def update_strategy_in_uow(
        self,
        uow: MaaUnitOfWork,
        *,
        strategy_id: UUID,
        expected_strategy_version: int,
        start_script_id: UUID,
        process_modules: Sequence[ProcessModuleInput],
        end_script_id: UUID,
        start_wait_after_ms: int,
        default_parameters: dict[str, Any],
        expected_impact_hash: str | None,
        correlation_id: UUID,
        now: datetime,
    ) -> StrategySaveResult:
        strategy = self._require_strategy(
            uow, strategy_id, expected_strategy_version, for_update=True
        )
        version, modules = self._build_version(
            uow,
            strategy=strategy,
            start_script_id=start_script_id,
            process_modules=process_modules,
            end_script_id=end_script_id,
            start_wait_after_ms=start_wait_after_ms,
            default_parameters=default_parameters,
            now=now,
        )
        # Saving affects future, not already materialized, task snapshots.
        # Keep the parameter for old internal callers, but no publication gate.
        existing = uow.strategy_versions.find_by_hash(
            strategy.strategy_id,
            version.manifest_hash,
        )
        reused = existing is not None
        if existing is not None:
            version = existing
            modules = uow.strategy_versions.list_modules(existing.strategy_version_id)
        else:
            uow.strategy_versions.add(version)
            uow.flush()
            uow.strategy_versions.add_modules(modules)

        if strategy.current_version_id == version.strategy_version_id:
            return StrategySaveResult(strategy, version, tuple(modules), True)
        published = uow.strategies.set_current_version(
            strategy.strategy_id,
            strategy.row_version,
            version.strategy_version_id,
            now,
        )
        if published is None:
            raise MaaDomainError("stale_strategy", "Strategy changed concurrently", 409)
        self._record_published(
            uow,
            published,
            version,
            previous_version_id=strategy.current_version_id,
            correlation_id=correlation_id,
            occurred_at=now,
        )
        return StrategySaveResult(published, version, tuple(modules), reused)

    def _build_version(
        self,
        uow: MaaUnitOfWork,
        *,
        strategy: StrategyRecord,
        start_script_id: UUID,
        process_modules: Sequence[ProcessModuleInput],
        end_script_id: UUID,
        start_wait_after_ms: int,
        default_parameters: dict[str, Any],
        now: datetime,
    ) -> tuple[StrategyVersionRecord, list[StrategyModuleRecord]]:
        if len(process_modules) > MAX_PROCESS_MODULES:
            raise MaaDomainError(
                "too_many_strategy_modules",
                f"A strategy can contain at most {MAX_PROCESS_MODULES} process modules",
                422,
            )
        waits = [start_wait_after_ms, *(item.wait_after_ms for item in process_modules)]
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= MAX_WAIT_AFTER_MS
            for value in waits
        ):
            raise MaaDomainError(
                "invalid_strategy_wait",
                "Module wait must be an integer from 0 to 14400000 milliseconds",
                422,
            )
        requested_ids = [
            start_script_id,
            *(item.script_id for item in process_modules),
            end_script_id,
        ]
        scripts = uow.scripts.find_active_by_ids(requested_ids)
        if set(scripts) != set(requested_ids):
            raise MaaDomainError(
                "strategy_script_missing",
                "Every strategy module must reference an active script",
                422,
            )
        ordered_scripts = [scripts[script_id] for script_id in requested_ids]
        self._validate_scripts(strategy, ordered_scripts)
        version_id = uuid4()
        modules = self._modules(
            version_id,
            ordered_scripts,
            waits,
        )
        manifest = build_strategy_manifest(
            strategy.application_id,
            modules,
            default_parameters=default_parameters,
        )
        try:
            manifest_hash = canonical_manifest_hash(manifest)
        except (TypeError, ValueError) as exc:
            raise MaaDomainError(
                "invalid_strategy_parameters",
                "Strategy default parameters must be JSON serializable",
                422,
            ) from exc
        return (
            StrategyVersionRecord(
                strategy_version_id=version_id,
                strategy_id=strategy.strategy_id,
                revision=uow.strategy_versions.next_revision(strategy.strategy_id),
                schema_version=1,
                manifest_hash=manifest_hash,
                manifest=manifest,
                created_at=now,
            ),
            modules,
        )

    @staticmethod
    def _require_strategy(
        uow: MaaUnitOfWork,
        strategy_id: UUID,
        expected_strategy_version: int,
        *,
        for_update: bool,
    ) -> StrategyRecord:
        strategy = uow.strategies.get_active(strategy_id, for_update=for_update)
        if strategy is None:
            raise MaaDomainError("strategy_not_found", "Strategy was not found", 404)
        if strategy.row_version != expected_strategy_version:
            raise MaaDomainError("stale_strategy", "Strategy changed concurrently", 409)
        return strategy

    @staticmethod
    def _preview_impact(
        uow: MaaUnitOfWork,
        strategy: StrategyRecord,
        proposed: StrategyVersionRecord,
    ) -> StrategyPublicationImpact:
        current_hash = None
        if strategy.current_version_id is not None:
            current = uow.strategy_versions.get(strategy.current_version_id)
            if current is None:
                raise MaaDomainError(
                    "strategy_version_missing",
                    "Current strategy version is unavailable",
                    409,
                )
            current_hash = current.manifest_hash
        content_changed = current_hash != proposed.manifest_hash
        schedules = (
            uow.schedule_impacts.list_active_future_impacts(
                source_module="maa",
                logical_content_ids=(f"strategy:{strategy.strategy_id}",),
            )
            if content_changed
            else []
        )
        fingerprint = {
            "strategy_id": str(strategy.strategy_id),
            "strategy_row_version": strategy.row_version,
            "current_version_id": (
                str(strategy.current_version_id) if strategy.current_version_id else None
            ),
            "proposed_manifest_hash": proposed.manifest_hash,
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
        return StrategyPublicationImpact(
            strategy_id=strategy.strategy_id,
            current_version_id=strategy.current_version_id,
            proposed_manifest_hash=proposed.manifest_hash,
            active_schedules=tuple(schedules),
            impact_hash=canonical_manifest_hash(fingerprint),
            content_changed=content_changed,
        )

    @staticmethod
    def _validate_scripts(strategy: StrategyRecord, scripts: Sequence[ScriptRecord]) -> None:
        if any(script.application_id != strategy.application_id for script in scripts):
            raise MaaDomainError(
                "strategy_application_mismatch",
                "Every strategy module must belong to the strategy application",
                422,
            )
        if any(
            script.status is not ScriptStatus.ACTIVE or script.current_version_id is None
            for script in scripts
        ):
            raise MaaDomainError(
                "strategy_script_unsaved",
                "Every strategy module must reference a saved script",
                422,
            )
        expected_types = [
            ScriptType.MODULE_START,
            *([ScriptType.MODULE_PROCESS] * (len(scripts) - 2)),
            ScriptType.MODULE_END,
        ]
        if any(
            script.script_type is not expected
            for script, expected in zip(scripts, expected_types, strict=True)
        ):
            raise MaaDomainError(
                "strategy_module_type_mismatch",
                "Strategy order must be start, zero or more process modules, then end",
                422,
            )

    @staticmethod
    def _modules(
        strategy_version_id: UUID,
        scripts: Sequence[ScriptRecord],
        waits: Sequence[int],
    ) -> list[StrategyModuleRecord]:
        last = len(scripts) - 1
        modules: list[StrategyModuleRecord] = []
        for position, script in enumerate(scripts):
            assert script.current_version_id is not None
            role = (
                StrategyModuleRole.START
                if position == 0
                else StrategyModuleRole.END
                if position == last
                else StrategyModuleRole.PROCESS
            )
            modules.append(
                StrategyModuleRecord(
                    strategy_version_id=strategy_version_id,
                    position=position,
                    module_role=role,
                    script_version_id=script.current_version_id,
                    wait_after_ms=0 if role is StrategyModuleRole.END else waits[position],
                )
            )
        return modules

    @staticmethod
    def _record_published(
        uow: MaaUnitOfWork,
        strategy: StrategyRecord,
        version: StrategyVersionRecord,
        *,
        previous_version_id: UUID | None,
        correlation_id: UUID,
        occurred_at: datetime,
    ) -> None:
        details = {
            "previous_version_id": (str(previous_version_id) if previous_version_id else None),
            "saved_version_id": str(version.strategy_version_id),
            "manifest_hash": version.manifest_hash,
            "row_version": strategy.row_version,
        }
        uow.audit.add(
            NewAuditEntry(
                audit_id=uuid4(),
                actor_type="operator",
                actor_id=None,
                action="maa.strategy.save",
                target_type="maa_strategy",
                target_id=strategy.strategy_id,
                correlation_id=correlation_id,
                details=details,
                summary="Maa strategy version saved",
            )
        )
        uow.outbox.add(
            NewOutboxEvent(
                event_id=uuid4(),
                event_type="maa.strategy.version-saved.v1",
                schema_version=1,
                aggregate_type="maa_strategy",
                aggregate_id=strategy.strategy_id,
                correlation_id=correlation_id,
                occurred_at=occurred_at,
                payload={"strategy_id": str(strategy.strategy_id), **details},
            )
        )
