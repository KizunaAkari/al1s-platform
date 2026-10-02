from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from al1s.adapters.postgres.mqtt_models import TerminalMqttSessionRow
from al1s.execution.mqtt_sessions import (
    MqttSessionStatus,
    TerminalMqttSessionRecord,
)


def _record(row: TerminalMqttSessionRow) -> TerminalMqttSessionRecord:
    return TerminalMqttSessionRecord(
        session_id=row.id,
        terminal_id=row.terminal_id,
        client_id=row.client_id,
        username=row.username,
        role_name=row.role_name,
        topic=row.topic,
        password_digest=row.password_digest,
        status=MqttSessionStatus(row.status),
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        configured_at=row.configured_at,
        revoked_at=row.revoked_at,
        row_version=row.row_version,
    )


class PostgresTerminalMqttSessionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_terminal(self, terminal_id: UUID) -> None:
        digest = hashlib.sha256(b"mqtt-session:" + terminal_id.bytes).digest()
        lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
        )

    def get_reusable(
        self, terminal_id: UUID, *, valid_after: datetime
    ) -> TerminalMqttSessionRecord | None:
        row = self._session.scalar(
            select(TerminalMqttSessionRow)
            .where(
                TerminalMqttSessionRow.terminal_id == terminal_id,
                TerminalMqttSessionRow.status.in_(
                    (MqttSessionStatus.PENDING.value, MqttSessionStatus.ACTIVE.value)
                ),
                TerminalMqttSessionRow.expires_at > valid_after,
            )
            .order_by(TerminalMqttSessionRow.issued_at.desc())
            .limit(1)
        )
        return _record(row) if row is not None else None

    def add(self, session: TerminalMqttSessionRecord) -> None:
        self._session.add(
            TerminalMqttSessionRow(
                id=session.session_id,
                terminal_id=session.terminal_id,
                client_id=session.client_id,
                username=session.username,
                role_name=session.role_name,
                topic=session.topic,
                password_digest=session.password_digest,
                status=session.status.value,
                issued_at=session.issued_at,
                expires_at=session.expires_at,
                configured_at=session.configured_at,
                revoked_at=session.revoked_at,
                row_version=session.row_version,
            )
        )

    def get_by_id(self, session_id: UUID) -> TerminalMqttSessionRecord | None:
        row = self._session.get(TerminalMqttSessionRow, session_id)
        return _record(row) if row is not None else None

    def mark_active(
        self, session_id: UUID, expected_version: int, *, configured_at: datetime
    ) -> TerminalMqttSessionRecord | None:
        row = self._session.scalar(
            update(TerminalMqttSessionRow)
            .where(
                TerminalMqttSessionRow.id == session_id,
                TerminalMqttSessionRow.row_version == expected_version,
                TerminalMqttSessionRow.status == MqttSessionStatus.PENDING.value,
            )
            .values(
                status=MqttSessionStatus.ACTIVE.value,
                configured_at=configured_at,
                row_version=TerminalMqttSessionRow.row_version + 1,
            )
            .returning(TerminalMqttSessionRow)
        )
        return _record(row) if row is not None else None

    def list_expired(
        self, *, now: datetime, limit: int
    ) -> Sequence[TerminalMqttSessionRecord]:
        rows = self._session.scalars(
            select(TerminalMqttSessionRow)
            .where(
                TerminalMqttSessionRow.status.in_(
                    (MqttSessionStatus.PENDING.value, MqttSessionStatus.ACTIVE.value)
                ),
                TerminalMqttSessionRow.expires_at <= now,
            )
            .order_by(TerminalMqttSessionRow.expires_at, TerminalMqttSessionRow.id)
            .limit(limit)
        ).all()
        return tuple(_record(row) for row in rows)

    def mark_expired(
        self, session_id: UUID, expected_version: int, *, expired_at: datetime
    ) -> bool:
        row = self._session.scalar(
            update(TerminalMqttSessionRow)
            .where(
                TerminalMqttSessionRow.id == session_id,
                TerminalMqttSessionRow.row_version == expected_version,
                TerminalMqttSessionRow.status.in_(
                    (MqttSessionStatus.PENDING.value, MqttSessionStatus.ACTIVE.value)
                ),
            )
            .values(
                status=MqttSessionStatus.EXPIRED.value,
                revoked_at=expired_at,
                row_version=TerminalMqttSessionRow.row_version + 1,
            )
            .returning(TerminalMqttSessionRow.id)
        )
        return row is not None
