from datetime import datetime
from uuid import UUID, uuid4

from al1s.kernel.types import NewAuditEntry, NewOutboxEvent
from al1s.maa.errors import MaaDomainError
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.types import ScriptRecord


def rename_script(
    uow: MaaUnitOfWork, *, script_id: UUID, expected_version: int,
    name: str, normalized_name: str, now: datetime, correlation_id: UUID,
) -> ScriptRecord:
    script = uow.scripts.get_active(script_id, for_update=True)
    if script is None:
        raise MaaDomainError("script_not_found", "Script was not found", 404)
    if script.row_version != expected_version:
        raise MaaDomainError("stale_script", "Script changed concurrently", 409)
    if script.name == name:
        return script
    updated = uow.scripts.update_name(script_id, expected_version, name, normalized_name, now)
    if updated is None:
        raise MaaDomainError("stale_script", "Script changed concurrently", 409)
    details = {"previous_name": script.name, "name": name, "row_version": updated.row_version}
    uow.audit.add(NewAuditEntry(
        audit_id=uuid4(), actor_type="operator", actor_id=None,
        action="maa.script.rename", target_type="maa_script", target_id=script_id,
        correlation_id=correlation_id, details=details, summary="Maa script renamed",
    ))
    uow.outbox.add(NewOutboxEvent(
        event_id=uuid4(), event_type="maa.script.renamed.v1", schema_version=1,
        aggregate_type="maa_script", aggregate_id=script_id, correlation_id=correlation_id,
        occurred_at=now, payload={"script_id": str(script_id), **details},
    ))
    return updated
