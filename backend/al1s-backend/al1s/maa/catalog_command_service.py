from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Never
from uuid import UUID, uuid4

from al1s.kernel.types import NewAuditEntry, NewOutboxEvent
from al1s.maa.errors import MaaDomainError
from al1s.maa.mutation_service import (
    MaaMutationService,
    MutationExecution,
    MutationOutcome,
)
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.types import ApplicationRecord

MaaUowFactory = Callable[[], MaaUnitOfWork]


class MaaCatalogCommandService:
    """Metadata commands whose replay receipt commits with the business mutation."""

    def __init__(
        self,
        uow_factory: MaaUowFactory,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._mutations = MaaMutationService(uow_factory, now=self._now)

    def rename_application(
        self,
        *,
        application_id: UUID,
        display_name: str,
        expected_row_version: int,
        idempotency_key: str,
        correlation_id: UUID,
    ) -> MutationExecution:
        normalized_name = display_name.strip()
        if not 1 <= len(normalized_name) <= 255:
            raise MaaDomainError(
                "invalid_application_name",
                "Application display name must contain 1 to 255 characters",
                422,
            )
        return self._mutations.execute(
            operation=f"maa.applications.rename:{application_id}",
            idempotency_key=idempotency_key,
            request_payload={
                "display_name": normalized_name,
                "expected_row_version": expected_row_version,
            },
            mutate=lambda uow: self._rename_application(
                uow,
                application_id=application_id,
                display_name=normalized_name,
                expected_row_version=expected_row_version,
                correlation_id=correlation_id,
            ),
        )

    def delete_application(
        self,
        *,
        application_id: UUID,
        expected_row_version: int,
        idempotency_key: str,
        correlation_id: UUID,
    ) -> MutationExecution:
        return self._mutations.execute(
            operation=f"maa.applications.delete:{application_id}",
            idempotency_key=idempotency_key,
            request_payload={"expected_row_version": expected_row_version},
            mutate=lambda uow: self._delete_application(
                uow,
                application_id=application_id,
                expected_row_version=expected_row_version,
                correlation_id=correlation_id,
            ),
        )

    def set_application_icon(
        self, *, application_id: UUID, icon_png: bytes | None,
        expected_row_version: int, idempotency_key: str, correlation_id: UUID,
    ) -> MutationExecution:
        return self._mutations.execute(
            operation=f"maa.applications.icon:{application_id}",
            idempotency_key=idempotency_key,
            request_payload={
                "expected_row_version": expected_row_version,
                "icon_sha256": sha256(icon_png).hexdigest() if icon_png is not None else None,
            },
            mutate=lambda uow: self._set_application_icon(
                uow, application_id=application_id, icon_png=icon_png,
                expected_row_version=expected_row_version, correlation_id=correlation_id,
            ),
        )

    def _set_application_icon(
        self, uow: MaaUnitOfWork, *, application_id: UUID,
        icon_png: bytes | None, expected_row_version: int, correlation_id: UUID,
    ) -> MutationOutcome:
        application = self._require_application(uow, application_id)
        if application.row_version != expected_row_version:
            self._raise_stale_application(expected_row_version)
        if icon_png is not None and application.icon_png is not None:
            raise MaaDomainError("application_icon_exists", "请先删除现有图标", 409)
        if icon_png is None and application.icon_png is None:
            updated = application
        else:
            changed = uow.applications.update_icon(
                application_id, expected_row_version, icon_png, self._now(),
            )
            if changed is None:
                self._raise_stale_application(expected_row_version)
            updated = changed
            self._record_change(
                uow, application=updated,
                action=("maa.application.icon.upload" if icon_png is not None
                        else "maa.application.icon.delete"),
                event_type="maa.application.icon.changed.v1",
                correlation_id=correlation_id,
                details={"row_version": updated.row_version, "has_icon": icon_png is not None},
            )
        return MutationOutcome(
            aggregate_type="maa_application", aggregate_id=application_id,
            response_status=200, response_body=self._application_body(updated),
        )

    def reorder_script(
        self, *, application_id: UUID, script_id: UUID, target_id: UUID,
        placement: str, idempotency_key: str, correlation_id: UUID,
    ) -> MutationExecution:
        if script_id == target_id or placement not in {"before", "after"}:
            raise MaaDomainError("invalid_script_order", "请选择其他脚本作为拖动目标。", 422)
        return self._mutations.execute(
            operation=f"maa.scripts.reorder:{application_id}",
            idempotency_key=idempotency_key,
            request_payload={
                "script_id": str(script_id), "target_id": str(target_id),
                "placement": placement,
            },
            mutate=lambda uow: self._reorder_script(
                uow, application_id=application_id, script_id=script_id,
                target_id=target_id, placement=placement,
                correlation_id=correlation_id,
            ),
        )

    def _reorder_script(
        self, uow: MaaUnitOfWork, *, application_id: UUID, script_id: UUID,
        target_id: UUID, placement: str, correlation_id: UUID,
    ) -> MutationOutcome:
        application = self._require_application(uow, application_id)
        if not uow.scripts.move_display_order(
            application_id=application_id, script_id=script_id,
            target_id=target_id, placement=placement,
        ):
            raise MaaDomainError("script_order_changed", "脚本列表已变化, 请刷新后重试。", 409)
        uow.audit.add(NewAuditEntry(
            audit_id=uuid4(), actor_type="operator", actor_id=None,
            action="maa.script.reorder", target_type="maa_script", target_id=script_id,
            correlation_id=correlation_id,
            details={
                "application_id": str(application_id), "target_id": str(target_id),
                "placement": placement,
            },
            summary="Maa script display order changed",
        ))
        return MutationOutcome(
            aggregate_type="maa_application", aggregate_id=application.application_id,
            response_status=204, response_body={},
        )

    def _rename_application(
        self,
        uow: MaaUnitOfWork,
        *,
        application_id: UUID,
        display_name: str,
        expected_row_version: int,
        correlation_id: UUID,
    ) -> MutationOutcome:
        application = self._require_application(uow, application_id)
        if application.row_version != expected_row_version:
            self._raise_stale_application(expected_row_version)
        if application.display_name == display_name:
            updated = application
        else:
            changed = uow.applications.update_display_name(
                application_id,
                expected_row_version,
                display_name,
                self._now(),
            )
            if changed is None:
                self._raise_stale_application(expected_row_version)
            updated = changed
            self._record_change(
                uow,
                application=updated,
                action="maa.application.rename",
                event_type="maa.application.renamed.v1",
                correlation_id=correlation_id,
                details={
                    "previous_display_name": application.display_name,
                    "display_name": updated.display_name,
                    "row_version": updated.row_version,
                },
            )
        return MutationOutcome(
            aggregate_type="maa_application",
            aggregate_id=updated.application_id,
            response_status=200,
            response_body=self._application_body(updated),
        )

    def _delete_application(
        self,
        uow: MaaUnitOfWork,
        *,
        application_id: UUID,
        expected_row_version: int,
        correlation_id: UUID,
    ) -> MutationOutcome:
        application = self._require_application(uow, application_id)
        if application.row_version != expected_row_version:
            self._raise_stale_application(expected_row_version)
        uow.application_deletions.lock_contents(application_id)
        blockers = uow.application_deletions.active_tasks(application_id)
        if blockers:
            raise MaaDomainError(
                "application_used_by_tasks",
                "分类被活动任务引用, 请先停止并清理这些任务。",
                409,
                context={
                    "task_ids": [str(item) for item in blockers[:50]],
                    "has_more": len(blockers) > 50,
                },
            )
        if uow.application_deletions.has_external_references(application_id):
            raise MaaDomainError(
                "application_external_reference", "分类脚本被其他分类引用, 无法删除", 409
            )
        script_count, strategy_count, _ = uow.application_deletions.counts(application_id)
        now = self._now()
        uow.application_deletions.soft_delete_contents(application_id, now)
        if not uow.applications.soft_delete(application_id, expected_row_version, now):
            self._raise_stale_application(expected_row_version)
        self._record_change(
            uow,
            application=application,
            action="maa.application.delete",
            event_type="maa.application.retired.v1",
            correlation_id=correlation_id,
            details={
                "row_version": expected_row_version + 1,
                "script_count": script_count,
                "strategy_count": strategy_count,
            },
            occurred_at=now,
        )
        return MutationOutcome(
            aggregate_type="maa_application",
            aggregate_id=application_id,
            response_status=204,
            response_body={},
        )

    @staticmethod
    def _require_application(uow: MaaUnitOfWork, application_id: UUID) -> ApplicationRecord:
        application = uow.applications.get_active(application_id, for_update=True)
        if application is None:
            raise MaaDomainError("application_not_found", "Application was not found", 404)
        return application

    @staticmethod
    def _raise_stale_application(expected_row_version: int) -> Never:
        raise MaaDomainError(
            "stale_application",
            "Application changed concurrently",
            409,
            context={"expected_row_version": expected_row_version},
        )

    @staticmethod
    def _application_body(application: ApplicationRecord) -> dict[str, Any]:
        return {
            "application_id": str(application.application_id),
            "package_name": application.package_name,
            "display_name": application.display_name,
            "row_version": application.row_version,
            "created_at": application.created_at.isoformat(),
            "updated_at": application.updated_at.isoformat(),
        }

    def _record_change(
        self,
        uow: MaaUnitOfWork,
        *,
        application: ApplicationRecord,
        action: str,
        event_type: str,
        correlation_id: UUID,
        details: dict[str, Any],
        occurred_at: datetime | None = None,
    ) -> None:
        timestamp = occurred_at or self._now()
        uow.audit.add(
            NewAuditEntry(
                audit_id=uuid4(),
                actor_type="operator",
                actor_id=None,
                action=action,
                target_type="maa_application",
                target_id=application.application_id,
                correlation_id=correlation_id,
                details=details,
                summary="Maa application metadata changed",
            )
        )
        uow.outbox.add(
            NewOutboxEvent(
                event_id=uuid4(),
                event_type=event_type,
                schema_version=1,
                aggregate_type="maa_application",
                aggregate_id=application.application_id,
                correlation_id=correlation_id,
                occurred_at=timestamp,
                payload={
                    "application_id": str(application.application_id),
                    **details,
                },
            )
        )
