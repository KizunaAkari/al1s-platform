"""One admission lock shared by upgrade reservation and work-creating transactions."""

from uuid import UUID

from sqlalchemy import exists, select, text
from sqlalchemy.orm import Session

from al1s.adapters.postgres.component_models import ComponentOperationRow
from al1s.adapters.postgres.device_path_models import DeviceAuthorityRow
from al1s.adapters.postgres.execution_models import TargetDeviceRow
from al1s.execution.errors import ConflictError

ACTIVE = ("accepted", "preparing", "switching", "verifying", "unknown")


def admission_lock(session: Session) -> None:
    # A constant PostgreSQL advisory namespace is clearer than an unneeded lock-row table.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
        {"scope": "al1s-component-admission"},
    )


class ComponentAdmission:
    def __init__(self, session: Session):
        self.session = session

    def admit(self, terminal: UUID | None = None, device: UUID | None = None) -> None:
        admission_lock(self.session)
        if device is not None and self.session.scalar(
            select(
                exists().where(
                    DeviceAuthorityRow.device_id == device,
                    DeviceAuthorityRow.state != "active",
                )
            )
        ):
            raise ConflictError("device_handoff_in_progress", "Device input authority is changing")
        if terminal is None and device is not None:
            terminal = self.session.scalar(
                select(TargetDeviceRow.managing_terminal_id).where(TargetDeviceRow.id == device)
            )
        scopes = ["platform"]
        if terminal:
            scopes.append(f"terminal:{terminal}")
        blocked = self.session.scalar(
            select(
                exists().where(
                    ComponentOperationRow.scope.in_(scopes), ComponentOperationRow.state.in_(ACTIVE)
                )
            )
        )
        if blocked:
            raise ConflictError("component_under_maintenance", "Component upgrade blocks new work")

    def platform_open(self) -> bool:
        try:
            self.admit()
            return True
        except ConflictError:
            return False
