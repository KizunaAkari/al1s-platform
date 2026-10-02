from datetime import datetime
from uuid import UUID, uuid4

from al1s.kernel.types import NewAuditEntry, NewOutboxEvent
from al1s.maa.errors import MaaDomainError
from al1s.maa.mutation_service import MutationOutcome
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.types import ScriptType


def delete_script(
    uow: MaaUnitOfWork,
    *,
    script_id: UUID,
    expected_version: int,
    now: datetime,
    correlation_id: UUID,
) -> MutationOutcome:
    # Fences the reference-pointer triggers' FOR SHARE lock on this target.
    script = uow.scripts.get_active(script_id, for_update=True)
    if script is None:
        raise MaaDomainError("script_not_found", "Script was not found", 404)
    if script.row_version != expected_version:
        raise MaaDomainError("stale_script", "Script changed concurrently", 409)
    if script.script_type is not ScriptType.MODULE_PROCESS:
        raise MaaDomainError(
            "lifecycle_script_delete_forbidden",
            "只有过程脚本可单独删除; 开始和结束脚本须随分类一起处理。",
            409,
        )
    referrers = uow.scripts.list_referrers(script_id, after_id=None, limit=51)
    if referrers:
        raise MaaDomainError(
            "script_in_use",
            "脚本仍被引用。请先修改并保存引用来源。",
            409,
            context={"referrers": referrers[:50], "has_more": len(referrers) > 50},
        )
    if uow.scripts.has_strategy_referrers(script_id):
        raise MaaDomainError(
            "script_used_by_strategy", "脚本仍被当前组合策略引用。请先修改策略。", 409
        )
    if uow.schedule_impacts.list_active_future_impacts(
        source_module="maa",
        logical_content_ids=(f"script:{script_id}",),
    ):
        raise MaaDomainError(
            "script_used_by_schedule", "脚本仍被活动计划引用。请先中止相关计划。", 409
        )
    if not uow.scripts.soft_delete(script_id, expected_version, now):
        raise MaaDomainError("stale_script", "Script changed concurrently", 409)
    details = {"script_id": str(script_id), "name": script.name}
    uow.audit.add(
        NewAuditEntry(
            audit_id=uuid4(),
            actor_type="operator",
            actor_id=None,
            action="maa.script.delete",
            target_type="maa_script",
            target_id=script_id,
            correlation_id=correlation_id,
            details=details,
            summary="Maa script soft deleted; immutable versions retained",
        )
    )
    uow.outbox.add(
        NewOutboxEvent(
            event_id=uuid4(),
            event_type="maa.script.deleted.v1",
            schema_version=1,
            aggregate_type="maa_script",
            aggregate_id=script_id,
            correlation_id=correlation_id,
            occurred_at=now,
            payload=details,
        )
    )
    return MutationOutcome(
        aggregate_type="maa_script",
        aggregate_id=script_id,
        response_status=200,
        response_body={"deleted": True},
    )
