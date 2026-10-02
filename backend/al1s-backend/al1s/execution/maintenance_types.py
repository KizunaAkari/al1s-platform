from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

ACTIVE = frozenset({"pending", "accepted", "executing", "recovering"})
FAILED = frozenset({"refused", "expired", "response_timeout", "recovery_timeout", "failed"})


@dataclass(frozen=True)
class MaintenanceRecord:
    command_id: UUID
    terminal_id: UUID
    action: str
    request_hash: str
    state: str
    submitted_at: datetime
    expires_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    remote_version: int
    late_state: str | None
    release_id: UUID | None = None

    def response(self) -> dict[str, object]:
        can_cancel = self.state == "accepted" or (
            self.state == "executing" and self.action == "upgrade_container"
            and self.error_code == "upgrade_downloading"
        )
        return {
            "command_id": str(self.command_id),
            "action": self.action,
            "state": self.state,
            "accepted_at": self.submitted_at.timestamp(),
            "expires_at": self.expires_at.timestamp(),
            "started_at": self.started_at.timestamp() if self.started_at else None,
            "error_code": self.error_code,
            "version": self.remote_version,
            "late_state": self.late_state,
            "release_id": str(self.release_id) if self.release_id else None,
            "can_cancel": can_cancel,
            "cancel_unavailable_reason": (
                None if can_cancel else "command_not_in_cancellable_phase"
            ),
        }
