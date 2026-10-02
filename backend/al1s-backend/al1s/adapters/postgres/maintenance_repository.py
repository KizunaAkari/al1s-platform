from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.maintenance_models import MaintenanceCommandRow as Row
from al1s.adapters.postgres.repositories import PostgresAuditRepository, PostgresOutboxRepository
from al1s.execution.errors import ConflictError, NotFoundError
from al1s.execution.host_management import HostCommand
from al1s.execution.maintenance_types import ACTIVE, FAILED, MaintenanceRecord
from al1s.kernel.types import NewAuditEntry, NewOutboxEvent


def record(row: Row) -> MaintenanceRecord:
    return MaintenanceRecord(
        row.id,
        row.terminal_id,
        row.action,
        row.request_hash,
        row.state,
        row.submitted_at,
        row.expires_at,
        row.started_at,
        row.completed_at,
        row.error_code,
        row.remote_version,
        row.late_state,
        row.release_id,
    )


def event(session: Session, row: Row, now: datetime) -> None:
    correlation = uuid4()
    PostgresAuditRepository(session, owner_module="maa").add(
        NewAuditEntry(
            audit_id=uuid4(),
            actor_type="system",
            actor_id=None,
            action="terminal.maintenance." + row.state,
            target_type="terminal",
            target_id=row.terminal_id,
            correlation_id=correlation,
            details={"command_id": str(row.id), "action": row.action, "state": row.state},
            summary="Terminal maintenance state recorded",
        )
    )
    if row.state in FAILED:
        PostgresOutboxRepository(session, owner_module="maa").add(
            NewOutboxEvent(
                event_id=uuid4(),
                event_type="terminal.maintenance_failed.v1",
                schema_version=1,
                aggregate_type="terminal",
                aggregate_id=row.terminal_id,
                correlation_id=correlation,
                occurred_at=now,
                payload={"command_id": str(row.id), "reason": row.state},
            )
        )


class MaintenanceRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def get(self, terminal: UUID, identity: UUID) -> MaintenanceRecord | None:
        with self.sessions() as session:
            row = session.get(Row, identity)
            return record(row) if row and row.terminal_id == terminal else None

    def latest(self, terminal: UUID) -> MaintenanceRecord | None:
        with self.sessions() as session:
            row = session.scalar(
                select(Row)
                .where(Row.terminal_id == terminal)
                .order_by(Row.submitted_at.desc(), Row.id.desc())
                .limit(1)
            )
            return record(row) if row else None

    def reserve(self, value: MaintenanceRecord) -> tuple[MaintenanceRecord, bool]:
        try:
            with self.sessions.begin() as session:
                row = Row(
                    id=value.command_id,
                    terminal_id=value.terminal_id,
                    action=value.action,
                    release_id=value.release_id,
                    request_hash=value.request_hash,
                    state="pending",
                    submitted_at=value.submitted_at,
                    expires_at=value.expires_at,
                    next_check_at=value.submitted_at + timedelta(seconds=20),
                    remote_version=0,
                )
                session.add(row)
                session.flush()
                event(session, row, value.submitted_at)
                return record(row), True
        except IntegrityError:
            existing = self.get(value.terminal_id, value.command_id)
            if existing and existing.request_hash == value.request_hash:
                return existing, False
            raise ConflictError(
                "maintenance_conflict", "Command identity conflict or another maintenance is active"
            ) from None

    def due(self, now: datetime) -> list[MaintenanceRecord]:
        with self.sessions.begin() as session:
            rows = session.scalars(
                select(Row)
                .where(
                    Row.next_check_at <= now,
                    or_(
                        Row.state.in_(ACTIVE),
                        and_(
                            Row.state.in_(("response_timeout", "recovery_timeout")),
                            Row.submitted_at > now - timedelta(days=7),
                            Row.late_state.is_(None),
                        ),
                    ),
                )
                .order_by(Row.next_check_at, Row.id)
                .limit(4)
                .with_for_update(skip_locked=True)
            ).all()
            for row in rows:
                row.next_check_at = now + timedelta(seconds=60 if row.state in ACTIVE else 3600)
            return [record(row) for row in rows]

    def observe(
        self,
        terminal: UUID,
        identity: UUID,
        now: datetime,
        remote: HostCommand | None = None,
        *,
        refused: bool = False,
    ) -> MaintenanceRecord:
        with self.sessions.begin() as session:
            row = session.scalar(
                select(Row).where(Row.id == identity, Row.terminal_id == terminal).with_for_update()
            )
            if row is None:
                raise NotFoundError("maintenance_command")
            previous = row.state
            self._apply(row, now, remote, refused)
            if previous != row.state:
                if row.state not in ACTIVE:
                    row.completed_at = now
                event(session, row, now)
            return record(row)

    @staticmethod
    def _apply(row: Row, now: datetime, remote: HostCommand | None, refused: bool) -> None:
        if remote and (
            remote.command_id != row.id
            or remote.action != row.action
            or remote.release_id != row.release_id
        ):
            raise ConflictError("maintenance_response_mismatch", "Command response is mismatched")
        if row.state not in ACTIVE:
            if remote and remote.late_state:
                row.late_state, row.late_observed_at = remote.late_state, now
            elif remote and remote.state not in ACTIVE and remote.state != row.state:
                row.late_state, row.late_observed_at = remote.state, now
            return
        if remote and remote.version <= row.remote_version:
            remote = None
        if remote and remote.version > row.remote_version:
            row.remote_version = remote.version
            if remote.started_at is not None and row.started_at is None:
                started = datetime.fromtimestamp(remote.started_at, UTC)
                if (
                    row.submitted_at - timedelta(seconds=30)
                    <= started
                    <= now + timedelta(seconds=30)
                ):
                    row.started_at = started
            # A remote result first observed after our deadline remains late evidence.
        budget = 1800 if row.action == "upgrade_container" else 600
        deadline = row.started_at + timedelta(seconds=budget) if row.started_at else row.expires_at
        if now >= deadline:
            row.state = "recovery_timeout" if row.started_at else "response_timeout"
            row.error_code = row.state
            if remote and remote.state not in ACTIVE:
                row.late_state, row.late_observed_at = remote.state, now
        elif refused:
            row.state, row.error_code = "refused", "host_management_rejected"
        elif remote:
            if remote.state in {"executing", "recovering", "succeeded"} and row.started_at is None:
                row.error_code = "invalid_execution_start_evidence"
            else:
                row.state, row.error_code = remote.state, remote.error_code
