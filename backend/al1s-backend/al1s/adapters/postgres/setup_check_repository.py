import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.execution_models import TerminalRow
from al1s.adapters.postgres.setup_check_models import TerminalSetupCheckRow as Row
from al1s.execution.errors import ConflictError, NotFoundError
from al1s.execution.terminal_setup_checks import SetupCheckRecord, SetupFacts


def record(row: Row) -> SetupCheckRecord:
    facts = None
    if row.status == "completed":
        facts = SetupFacts(
            bool(row.secure_lock),
            bool(row.provider_ready),
            bool(row.battery_exempt),
            bool(row.notifications_allowed),
            bool(row.wireless_recovery_enabled),
            row.manufacturer_policy or "unverifiable",
        )
    return SetupCheckRecord(
        row.id,
        row.terminal_id,
        row.status,
        row.source,
        row.requested_at,
        row.claimed_at,
        row.instance_id,
        row.completed_at,
        facts,
    )


class SetupCheckRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def request(self, terminal: UUID, manual: bool, now: datetime) -> SetupCheckRecord | None:
        with self._sessions.begin() as tx:
            endpoint = tx.scalar(
                select(TerminalRow)
                .where(TerminalRow.id == terminal, TerminalRow.deleted_at.is_(None))
                .with_for_update()
            )
            if endpoint is None or endpoint.terminal_type != "android":
                raise NotFoundError("android_terminal")
            active = tx.scalar(
                select(Row)
                .where(Row.terminal_id == terminal, Row.status.in_(("pending", "claimed")))
                .limit(1)
            )
            if active:
                return record(active)
            latest = tx.scalar(
                select(Row)
                .where(Row.terminal_id == terminal, Row.status == "completed")
                .order_by(Row.completed_at.desc(), Row.id.desc())
                .limit(1)
            )
            if (
                not manual
                and latest
                and latest.completed_at
                and now < latest.completed_at + timedelta(hours=24)
            ):
                return None
            row = Row(
                id=uuid4(),
                terminal_id=terminal,
                status="pending",
                source="manual" if manual else "automatic",
                requested_at=now,
                row_version=1,
            )
            tx.add(row)
            tx.flush()
            return record(row)

    def _owned(self, tx: Session, terminal: UUID, request: UUID) -> Row:
        row = tx.scalar(
            select(Row).where(Row.id == request, Row.terminal_id == terminal).with_for_update()
        )
        if row is None:
            raise NotFoundError("setup_check")
        return row

    def claim(
        self, terminal: UUID, request: UUID, instance: UUID, now: datetime
    ) -> SetupCheckRecord:
        with self._sessions.begin() as tx:
            row = self._owned(tx, terminal, request)
            if row.status == "completed":
                return record(row)
            if (
                row.status == "claimed"
                and row.instance_id != instance
                and row.claimed_at
                and now < row.claimed_at + timedelta(seconds=60)
            ):
                raise ConflictError("setup_check_busy", "A setup check is already running")
            row.status, row.instance_id, row.claimed_at = "claimed", instance, now
            row.row_version += 1
            return record(row)

    def complete(
        self, terminal: UUID, request: UUID, instance: UUID, facts: SetupFacts, now: datetime
    ) -> SetupCheckRecord:
        values = asdict(facts)
        digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        with self._sessions.begin() as tx:
            row = self._owned(tx, terminal, request)
            if row.status == "completed":
                if row.result_hash != digest or row.instance_id != instance:
                    raise ConflictError(
                        "setup_check_report_conflict", "Completed check report differs"
                    )
                return record(row)
            if row.status != "claimed" or row.instance_id != instance:
                raise ConflictError(
                    "setup_check_instance_changed", "Setup check generation changed"
                )
            for key, value in values.items():
                setattr(row, key, value)
            row.status, row.completed_at, row.result_hash = "completed", now, digest
            row.row_version += 1
            return record(row)

    def latest(self, terminal: UUID) -> SetupCheckRecord | None:
        with self._sessions() as tx:
            row = tx.scalar(
                select(Row)
                .where(Row.terminal_id == terminal)
                .order_by(Row.requested_at.desc(), Row.id.desc())
                .limit(1)
            )
            return record(row) if row else None
