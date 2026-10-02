"""Maa persistence for PostgresMaaQuickTestSessionRepository."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from al1s.adapters.postgres.maa_models import (
    MaaQuickTestBlobRow,
    MaaQuickTestEventRow,
    MaaQuickTestSessionRow,
)
from al1s.adapters.postgres.maa_repository_records import (
    _quick_test_record,
)
from al1s.adapters.postgres.target_control import lock_target_assignment
from al1s.execution.scheduling_types import SnapshotBlobReference
from al1s.maa.types import (
    QuickTestEventRecord,
    QuickTestSessionRecord,
    QuickTestSessionStatus,
)


class PostgresMaaQuickTestSessionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        session: QuickTestSessionRecord,
        blobs: Sequence[SnapshotBlobReference],
    ) -> None:
        lock_target_assignment(self._session, session.target_device_id, session.terminal_id)
        session_row = MaaQuickTestSessionRow(
            id=session.session_id,
            script_id=session.script_id,
            script_version_id=session.script_version_id,
            manifest_hash=session.manifest_hash,
            definition_hash=session.definition_hash,
            definition=session.definition,
            terminal_id=session.terminal_id,
            target_device_id=session.target_device_id,
            status=session.status.value,
            request_idempotency_key=session.request_idempotency_key,
            expires_at=session.expires_at,
            claimed_at=session.claimed_at,
            started_at=session.started_at,
            cancel_requested_at=session.cancel_requested_at,
            completed_at=session.completed_at,
            qualification_receipt_id=session.qualification_receipt_id,
            created_at=session.created_at,
            row_version=session.row_version,
        )
        self._session.add(session_row)
        # No ORM relationship joins these aggregate rows. Flush the parent first so
        # SQLAlchemy cannot schedule the batched FK children ahead of the session.
        self._session.flush()
        self._session.add_all(
            MaaQuickTestBlobRow(
                session_id=session.session_id,
                blob_id=item.blob_id,
                resource_key=item.resource_key,
                role=item.role,
                ordinal=ordinal,
            )
            for ordinal, item in enumerate(blobs)
        )

    def get(self, session_id: UUID, *, for_update: bool = False) -> QuickTestSessionRecord | None:
        statement = select(MaaQuickTestSessionRow).where(MaaQuickTestSessionRow.id == session_id)
        if for_update:
            statement = statement.with_for_update()
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _quick_test_record(row)

    def find_by_idempotency(
        self, script_version_id: UUID, idempotency_key: str
    ) -> QuickTestSessionRecord | None:
        statement = select(MaaQuickTestSessionRow).where(
            MaaQuickTestSessionRow.script_version_id == script_version_id,
            MaaQuickTestSessionRow.request_idempotency_key == idempotency_key,
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _quick_test_record(row)

    def list_pending(
        self, terminal_id: UUID, now: datetime, *, limit: int
    ) -> list[QuickTestSessionRecord]:
        statement = (
            select(MaaQuickTestSessionRow)
            .where(
                MaaQuickTestSessionRow.terminal_id == terminal_id,
                MaaQuickTestSessionRow.status.in_(
                    (
                        QuickTestSessionStatus.ISSUED.value,
                        QuickTestSessionStatus.CLAIMED.value,
                    )
                ),
                MaaQuickTestSessionRow.expires_at > now,
            )
            .order_by(MaaQuickTestSessionRow.created_at, MaaQuickTestSessionRow.id)
            .limit(limit)
        )
        return [_quick_test_record(row) for row in self._session.execute(statement).scalars().all()]

    def claim(
        self,
        session_id: UUID,
        expected_version: int,
        terminal_id: UUID,
        claimed_at: datetime,
    ) -> QuickTestSessionRecord | None:
        statement = (
            update(MaaQuickTestSessionRow)
            .where(
                MaaQuickTestSessionRow.id == session_id,
                MaaQuickTestSessionRow.terminal_id == terminal_id,
                MaaQuickTestSessionRow.row_version == expected_version,
                MaaQuickTestSessionRow.status == QuickTestSessionStatus.ISSUED.value,
                MaaQuickTestSessionRow.expires_at > claimed_at,
            )
            .values(
                status=QuickTestSessionStatus.CLAIMED.value,
                claimed_at=claimed_at,
                row_version=MaaQuickTestSessionRow.row_version + 1,
            )
            .returning(MaaQuickTestSessionRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _quick_test_record(row)

    def complete(
        self,
        session_id: UUID,
        expected_version: int,
        qualification_receipt_id: UUID,
        completed_at: datetime,
    ) -> QuickTestSessionRecord | None:
        statement = (
            update(MaaQuickTestSessionRow)
            .where(
                MaaQuickTestSessionRow.id == session_id,
                MaaQuickTestSessionRow.row_version == expected_version,
                MaaQuickTestSessionRow.status == QuickTestSessionStatus.CLAIMED.value,
            )
            .values(
                status=QuickTestSessionStatus.COMPLETED.value,
                completed_at=completed_at,
                qualification_receipt_id=qualification_receipt_id,
                row_version=MaaQuickTestSessionRow.row_version + 1,
            )
            .returning(MaaQuickTestSessionRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _quick_test_record(row)

    def expire(
        self, session_id: UUID, expected_version: int, expired_at: datetime
    ) -> QuickTestSessionRecord | None:
        statement = (
            update(MaaQuickTestSessionRow)
            .where(
                MaaQuickTestSessionRow.id == session_id,
                MaaQuickTestSessionRow.row_version == expected_version,
                MaaQuickTestSessionRow.status.in_(
                    (
                        QuickTestSessionStatus.ISSUED.value,
                        QuickTestSessionStatus.CLAIMED.value,
                    )
                ),
            )
            .values(
                status=QuickTestSessionStatus.EXPIRED.value,
                completed_at=expired_at,
                row_version=MaaQuickTestSessionRow.row_version + 1,
            )
            .returning(MaaQuickTestSessionRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return None if row is None else _quick_test_record(row)

    def request_cancel(
        self, session_id: UUID, expected_version: int, now: datetime
    ) -> QuickTestSessionRecord | None:
        row = self._session.execute(
            update(MaaQuickTestSessionRow)
            .where(
                MaaQuickTestSessionRow.id == session_id,
                MaaQuickTestSessionRow.row_version == expected_version,
                MaaQuickTestSessionRow.status.in_(("issued", "claimed")),
                MaaQuickTestSessionRow.cancel_requested_at.is_(None),
            )
            .values(
                status=case((MaaQuickTestSessionRow.status == "issued", "cancelled"),
                            else_="claimed"),
                completed_at=case((MaaQuickTestSessionRow.status == "issued", now),
                                  else_=MaaQuickTestSessionRow.completed_at),
                cancel_requested_at=now,
                row_version=MaaQuickTestSessionRow.row_version + 1,
            )
            .returning(MaaQuickTestSessionRow)
        ).scalar_one_or_none()
        return _quick_test_record(row) if row is not None else None

    def append_events(
        self, session_id: UUID, events: Sequence[QuickTestEventRecord], cutoff: datetime
    ) -> int:
        last = self._session.scalar(
            select(MaaQuickTestSessionRow.event_last_sequence).where(
                MaaQuickTestSessionRow.id == session_id
            )
        ) or 0
        replayed = [event for event in events if event.sequence <= last]
        if replayed:
            existing = {
                row.sequence: row for row in self._session.scalars(
                    select(MaaQuickTestEventRow).where(
                        MaaQuickTestEventRow.session_id == session_id,
                        MaaQuickTestEventRow.sequence.in_([event.sequence for event in replayed]),
                    )
                )
            }
            if any(
                ((row := existing.get(event.sequence)) is None
                 and event.created_at > cutoff)
                or (row is not None and (row.kind, row.step_number, row.code) != (
                    event.kind, event.step_number, event.code
                ))
                for event in replayed
            ):
                raise ValueError("quick_test_event_replay_conflict")
        incoming = [event for event in events if event.sequence > last]
        if incoming and last == 0 and incoming[0].kind != "started":
            raise ValueError("quick_test_started_event_required")
        if incoming and [event.sequence for event in incoming] != list(
            range(last + 1, last + 1 + len(incoming))
        ):
            raise ValueError("quick_test_event_sequence_gap")
        self._session.add_all(
            MaaQuickTestEventRow(
                session_id=session_id, sequence=event.sequence, kind=event.kind,
                step_number=event.step_number, code=event.code,
                created_at=event.created_at,
            )
            for event in incoming if event.created_at > cutoff
        )
        if incoming and last == 0 and incoming[0].kind == "started":
            self._session.execute(
                update(MaaQuickTestSessionRow)
                .where(MaaQuickTestSessionRow.id == session_id,
                       MaaQuickTestSessionRow.started_at.is_(None))
                .values(started_at=incoming[0].created_at)
            )
        accepted = last + len(incoming)
        if incoming:
            self._session.execute(
                update(MaaQuickTestSessionRow)
                .where(MaaQuickTestSessionRow.id == session_id)
                .values(event_last_sequence=accepted)
            )
        return accepted

    def list_events(
        self, session_id: UUID, *, after: int, limit: int
    ) -> list[QuickTestEventRecord]:
        rows = self._session.scalars(
            select(MaaQuickTestEventRow).where(
                MaaQuickTestEventRow.session_id == session_id,
                MaaQuickTestEventRow.sequence > after,
            ).order_by(MaaQuickTestEventRow.sequence).limit(limit)
        ).all()
        return [QuickTestEventRecord(
            session_id=row.session_id, sequence=row.sequence, kind=row.kind,
            step_number=row.step_number, code=row.code, created_at=row.created_at,
        ) for row in rows]
