"""Serialize new control work with target ownership changes."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.execution_models import TargetDeviceRow
from al1s.execution.errors import ConflictError


def lock_target_assignment(session: Session, device_id: UUID | None, terminal_id: UUID) -> None:
    if device_id is None:
        return
    manager = session.scalar(
        select(TargetDeviceRow.managing_terminal_id)
        .where(
            TargetDeviceRow.id == device_id,
            TargetDeviceRow.deleted_at.is_(None),
            TargetDeviceRow.mode.in_(("mounted", "standalone")),
        )
        .with_for_update()
    )
    if manager != terminal_id:
        raise ConflictError(
            "target_terminal_mismatch", "Target assignment changed; retry with its current terminal"
        )
