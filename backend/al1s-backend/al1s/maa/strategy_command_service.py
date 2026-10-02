from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Never
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from al1s.kernel.types import NewAuditEntry, NewOutboxEvent
from al1s.maa.errors import MaaDomainError
from al1s.maa.mutation_service import (
    MaaMutationService,
    MutationExecution,
    MutationOutcome,
)
from al1s.maa.naming import normalize_asset_name
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.strategy_service import MaaStrategyService, StrategySaveResult
from al1s.maa.types import ProcessModuleInput, StrategyRecord

MaaUowFactory = Callable[[], MaaUnitOfWork]


class MaaStrategyCommandService:
    """Replay-safe strategy publication commands over immutable versions."""

    def __init__(
        self,
        uow_factory: MaaUowFactory,
        *,
        strategy_service: MaaStrategyService | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._mutations = MaaMutationService(uow_factory, now=self._now)
        self._strategies = strategy_service or MaaStrategyService(uow_factory, now=self._now)

    def create_strategy(
        self,
        *,
        application_id: UUID,
        name: str,
        start_script_id: UUID,
        process_modules: Sequence[ProcessModuleInput],
        end_script_id: UUID,
        start_wait_after_ms: int,
        default_parameters: dict[str, Any],
        idempotency_key: str,
        correlation_id: UUID,
    ) -> MutationExecution:
        return self._mutations.execute(
            operation=f"maa.strategies.create:{application_id}",
            idempotency_key=idempotency_key,
            request_payload=self._request_payload(
                application_id=application_id,
                name=name,
                start_script_id=start_script_id,
                process_modules=process_modules,
                end_script_id=end_script_id,
                start_wait_after_ms=start_wait_after_ms,
                default_parameters=default_parameters,
            ),
            mutate=lambda uow: self._create_strategy(
                uow,
                application_id=application_id,
                name=name,
                start_script_id=start_script_id,
                process_modules=process_modules,
                end_script_id=end_script_id,
                start_wait_after_ms=start_wait_after_ms,
                default_parameters=default_parameters,
                correlation_id=correlation_id,
            ),
            integrity_error_mapper=self._strategy_name_conflict,
        )

    def publish_strategy(
        self,
        *,
        strategy_id: UUID,
        expected_row_version: int,
        start_script_id: UUID,
        process_modules: Sequence[ProcessModuleInput],
        end_script_id: UUID,
        start_wait_after_ms: int,
        default_parameters: dict[str, Any],
        expected_impact_hash: str | None,
        idempotency_key: str,
        correlation_id: UUID,
    ) -> MutationExecution:
        return self._mutations.execute(
            operation=f"maa.strategies.publish:{strategy_id}",
            idempotency_key=idempotency_key,
            request_payload={
                **self._request_payload(
                    application_id=None,
                    name=None,
                    start_script_id=start_script_id,
                    process_modules=process_modules,
                    end_script_id=end_script_id,
                    start_wait_after_ms=start_wait_after_ms,
                    default_parameters=default_parameters,
                ),
                "expected_row_version": expected_row_version,
                "expected_impact_hash": expected_impact_hash,
            },
            mutate=lambda uow: self._publish_strategy(
                uow,
                strategy_id=strategy_id,
                expected_row_version=expected_row_version,
                start_script_id=start_script_id,
                process_modules=process_modules,
                end_script_id=end_script_id,
                start_wait_after_ms=start_wait_after_ms,
                default_parameters=default_parameters,
                expected_impact_hash=expected_impact_hash,
                correlation_id=correlation_id,
            ),
        )

    def save_strategy(
        self, *, strategy_id: UUID, expected_row_version: int,
        start_script_id: UUID, process_modules: Sequence[ProcessModuleInput],
        end_script_id: UUID, start_wait_after_ms: int,
        default_parameters: dict[str, Any], idempotency_key: str,
        correlation_id: UUID,
    ) -> MutationExecution:
        return self._mutations.execute(
            operation=f"maa.strategies.definition.save:{strategy_id}",
            idempotency_key=idempotency_key,
            request_payload={
                **self._request_payload(
                    application_id=None, name=None, start_script_id=start_script_id,
                    process_modules=process_modules, end_script_id=end_script_id,
                    start_wait_after_ms=start_wait_after_ms,
                    default_parameters=default_parameters,
                ),
                "expected_row_version": expected_row_version,
            },
            mutate=lambda uow: self._publish_strategy(
                uow, strategy_id=strategy_id, expected_row_version=expected_row_version,
                start_script_id=start_script_id, process_modules=process_modules,
                end_script_id=end_script_id, start_wait_after_ms=start_wait_after_ms,
                default_parameters=default_parameters, expected_impact_hash=None,
                correlation_id=correlation_id,
            ),
        )

    def rename_strategy(
        self,
        *,
        strategy_id: UUID,
        name: str,
        expected_row_version: int,
        idempotency_key: str,
        correlation_id: UUID,
    ) -> MutationExecution:
        normalized_name = normalize_asset_name(name, asset_kind="strategy")
        display_name = name.strip()
        return self._mutations.execute(
            operation=f"maa.strategies.rename:{strategy_id}",
            idempotency_key=idempotency_key,
            request_payload={
                "name": display_name,
                "expected_row_version": expected_row_version,
            },
            mutate=lambda uow: self._rename_strategy(
                uow,
                strategy_id=strategy_id,
                name=display_name,
                normalized_name=normalized_name,
                expected_row_version=expected_row_version,
                correlation_id=correlation_id,
            ),
            integrity_error_mapper=self._strategy_name_conflict,
        )

    def delete_strategy(
        self,
        *,
        strategy_id: UUID,
        expected_row_version: int,
        idempotency_key: str,
        correlation_id: UUID,
    ) -> MutationExecution:
        return self._mutations.execute(
            operation=f"maa.strategies.delete:{strategy_id}",
            idempotency_key=idempotency_key,
            request_payload={"expected_row_version": expected_row_version},
            mutate=lambda uow: self._delete_strategy(
                uow,
                strategy_id=strategy_id,
                expected_row_version=expected_row_version,
                correlation_id=correlation_id,
            ),
        )

    def _create_strategy(
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
    ) -> MutationOutcome:
        result = self._strategies.create_strategy_in_uow(
            uow,
            application_id=application_id,
            name=name,
            start_script_id=start_script_id,
            process_modules=process_modules,
            end_script_id=end_script_id,
            start_wait_after_ms=start_wait_after_ms,
            default_parameters=default_parameters,
            correlation_id=correlation_id,
            now=self._now(),
        )
        return self._outcome(result, status=201)

    def _publish_strategy(
        self,
        uow: MaaUnitOfWork,
        *,
        strategy_id: UUID,
        expected_row_version: int,
        start_script_id: UUID,
        process_modules: Sequence[ProcessModuleInput],
        end_script_id: UUID,
        start_wait_after_ms: int,
        default_parameters: dict[str, Any],
        expected_impact_hash: str | None,
        correlation_id: UUID,
    ) -> MutationOutcome:
        result = self._strategies.update_strategy_in_uow(
            uow,
            strategy_id=strategy_id,
            expected_strategy_version=expected_row_version,
            start_script_id=start_script_id,
            process_modules=process_modules,
            end_script_id=end_script_id,
            start_wait_after_ms=start_wait_after_ms,
            default_parameters=default_parameters,
            expected_impact_hash=expected_impact_hash,
            correlation_id=correlation_id,
            now=self._now(),
        )
        return self._outcome(result, status=200)

    def _rename_strategy(
        self,
        uow: MaaUnitOfWork,
        *,
        strategy_id: UUID,
        name: str,
        normalized_name: str,
        expected_row_version: int,
        correlation_id: UUID,
    ) -> MutationOutcome:
        strategy = self._require_strategy(uow, strategy_id, expected_row_version)
        if strategy.name == name:
            updated = strategy
        else:
            now = self._now()
            changed = uow.strategies.update_name(
                strategy_id,
                expected_row_version,
                name,
                normalized_name,
                now,
            )
            if changed is None:
                self._raise_stale_strategy()
            updated = changed
            self._record_metadata_change(
                uow,
                strategy=updated,
                action="maa.strategy.rename",
                event_type="maa.strategy.renamed.v1",
                correlation_id=correlation_id,
                occurred_at=now,
                details={
                    "previous_name": strategy.name,
                    "name": updated.name,
                    "row_version": updated.row_version,
                },
            )
        return MutationOutcome(
            aggregate_type="maa_strategy",
            aggregate_id=strategy_id,
            response_status=200,
            response_body=self._strategy_body(updated),
        )

    def _delete_strategy(
        self,
        uow: MaaUnitOfWork,
        *,
        strategy_id: UUID,
        expected_row_version: int,
        correlation_id: UUID,
    ) -> MutationOutcome:
        strategy = self._require_strategy(uow, strategy_id, expected_row_version)
        schedules = uow.schedule_impacts.list_active_future_impacts(
            source_module="maa",
            logical_content_ids=(f"strategy:{strategy_id}",),
        )
        if schedules:
            raise MaaDomainError(
                "strategy_has_active_schedules",
                "Strategy cannot be deleted while active schedules have future rounds",
                409,
                context={"active_schedule_count": len(schedules)},
            )
        now = self._now()
        if not uow.strategies.soft_delete(strategy_id, expected_row_version, now):
            self._raise_stale_strategy()
        self._record_metadata_change(
            uow,
            strategy=strategy,
            action="maa.strategy.delete",
            event_type="maa.strategy.retired.v1",
            correlation_id=correlation_id,
            occurred_at=now,
            details={"row_version": expected_row_version + 1},
        )
        return MutationOutcome(
            aggregate_type="maa_strategy",
            aggregate_id=strategy_id,
            response_status=204,
            response_body={},
        )

    @staticmethod
    def _request_payload(
        *,
        application_id: UUID | None,
        name: str | None,
        start_script_id: UUID,
        process_modules: Sequence[ProcessModuleInput],
        end_script_id: UUID,
        start_wait_after_ms: int,
        default_parameters: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "start_script_id": str(start_script_id),
            "process_modules": [
                {
                    "script_id": str(item.script_id),
                    "wait_after_ms": item.wait_after_ms,
                }
                for item in process_modules
            ],
            "end_script_id": str(end_script_id),
            "start_wait_after_ms": start_wait_after_ms,
            "default_parameters": default_parameters,
        }
        if application_id is not None:
            payload["application_id"] = str(application_id)
        if name is not None:
            payload["name"] = name.strip()
        return payload

    @classmethod
    def _outcome(cls, result: StrategySaveResult, *, status: int) -> MutationOutcome:
        strategy = result.strategy
        version = result.version
        return MutationOutcome(
            aggregate_type="maa_strategy",
            aggregate_id=strategy.strategy_id,
            response_status=status,
            response_body={
                "strategy": cls._strategy_body(strategy),
                "published_version": {
                    "strategy_version_id": str(version.strategy_version_id),
                    "revision": version.revision,
                    "schema_version": version.schema_version,
                    "manifest_hash": version.manifest_hash,
                    "created_at": version.created_at.isoformat(),
                },
                "modules": [
                    {
                        "position": item.position,
                        "module_role": item.module_role.value,
                        "script_version_id": str(item.script_version_id),
                        "wait_after_ms": item.wait_after_ms,
                    }
                    for item in result.modules
                ],
                "reused_version": result.reused_version,
            },
        )

    @staticmethod
    def _strategy_body(strategy: StrategyRecord) -> dict[str, Any]:
        return {
            "strategy_id": str(strategy.strategy_id),
            "application_id": str(strategy.application_id),
            "name": strategy.name,
            "status": strategy.status.value,
            "current_version_id": (
                str(strategy.current_version_id) if strategy.current_version_id else None
            ),
            "row_version": strategy.row_version,
            "created_at": strategy.created_at.isoformat(),
            "updated_at": strategy.updated_at.isoformat(),
        }

    @staticmethod
    def _require_strategy(
        uow: MaaUnitOfWork,
        strategy_id: UUID,
        expected_row_version: int,
    ) -> StrategyRecord:
        strategy = uow.strategies.get_active(strategy_id, for_update=True)
        if strategy is None:
            raise MaaDomainError("strategy_not_found", "Strategy was not found", 404)
        if strategy.row_version != expected_row_version:
            MaaStrategyCommandService._raise_stale_strategy()
        return strategy

    @staticmethod
    def _raise_stale_strategy() -> Never:
        raise MaaDomainError("stale_strategy", "Strategy changed concurrently", 409)

    @staticmethod
    def _record_metadata_change(
        uow: MaaUnitOfWork,
        *,
        strategy: StrategyRecord,
        action: str,
        event_type: str,
        correlation_id: UUID,
        occurred_at: datetime,
        details: dict[str, Any],
    ) -> None:
        uow.audit.add(
            NewAuditEntry(
                audit_id=uuid4(),
                actor_type="operator",
                actor_id=None,
                action=action,
                target_type="maa_strategy",
                target_id=strategy.strategy_id,
                correlation_id=correlation_id,
                details=details,
                summary="Maa strategy metadata changed",
            )
        )
        uow.outbox.add(
            NewOutboxEvent(
                event_id=uuid4(),
                event_type=event_type,
                schema_version=1,
                aggregate_type="maa_strategy",
                aggregate_id=strategy.strategy_id,
                correlation_id=correlation_id,
                occurred_at=occurred_at,
                payload={"strategy_id": str(strategy.strategy_id), **details},
            )
        )

    @staticmethod
    def _strategy_name_conflict(_error: IntegrityError) -> MaaDomainError:
        return MaaDomainError(
            "strategy_name_conflict",
            "An active strategy with the same application and name already exists",
            409,
        )
