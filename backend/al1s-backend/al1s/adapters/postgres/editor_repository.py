from dataclasses import asdict
from types import TracebackType
from typing import Self, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.editor_models import EditorSessionRow as Row
from al1s.adapters.postgres.execution_models import (
    TargetDeviceRow,
    TerminalCapabilityProfileRow,
    TerminalRow,
)
from al1s.execution.editor_sessions import EditorSession, EditorStatus
from al1s.execution.errors import ConflictError, NotFoundError

ACTIVE = ("pending", "active", "closing")


def record(row: Row) -> EditorSession:
    return EditorSession(
        row.id,
        row.terminal_id,
        row.device_id,
        row.request_id,
        EditorStatus(row.status),
        row.created_at,
        row.create_deadline,
        row.updated_at,
        row.row_version,
        row.ciphertext,
        row.key_id,
        row.terminal_instance_id,
        row.error_code,
    )


class EditorSqlTransaction:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def __enter__(self) -> Self:
        self._session = self._sessions()
        self._session.begin()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if kind is None:
                self._session.commit()
            else:
                self._session.rollback()
        except IntegrityError as error:
            self._session.rollback()
            raise ConflictError(
                "editor_concurrent_change", "Editor session changed concurrently"
            ) from error
        finally:
            self._session.close()

    def target_terminal(self, device_id: UUID, *, lock: bool = False) -> UUID:
        query = (
            select(TargetDeviceRow.managing_terminal_id, TerminalCapabilityProfileRow.provider_keys)
            .join(TerminalRow, TargetDeviceRow.managing_terminal_id == TerminalRow.id)
            .outerjoin(
                TerminalCapabilityProfileRow,
                TerminalRow.current_capability_profile_id == TerminalCapabilityProfileRow.id,
            )
            .where(
                TargetDeviceRow.id == device_id,
                TargetDeviceRow.mode == "mounted",
                TargetDeviceRow.deleted_at.is_(None),
                TerminalRow.deleted_at.is_(None),
                TerminalRow.terminal_type == "linux",
            )
        )
        if lock:
            query = query.with_for_update(of=TargetDeviceRow)
        result = self._session.execute(query).one_or_none()
        if result is None:
            raise NotFoundError("mounted_linux_device")
        terminal_id, providers = result
        if lock and "editor-session-v1" not in (providers or []):
            raise ConflictError(
                "editor_capability_unavailable", "Terminal editor capability unavailable"
            )
        return cast(UUID, terminal_id)

    def by_request(self, request_id: UUID) -> EditorSession | None:
        row = self._session.execute(
            select(Row).where(Row.request_id == request_id)
        ).scalar_one_or_none()
        return record(row) if row else None

    def by_device(self, device_id: UUID) -> EditorSession | None:
        row = self._session.execute(
            select(Row).where(Row.device_id == device_id, Row.status.in_(ACTIVE)).with_for_update()
        ).scalar_one_or_none()
        return record(row) if row else None

    def get(self, session_id: UUID) -> EditorSession:
        row = self._session.execute(
            select(Row).where(Row.id == session_id).with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("editor_session")
        return record(row)

    def put(self, session: EditorSession, previous: EditorSession | None) -> None:
        if session == previous:
            return
        values = asdict(session)
        values["id"] = values.pop("session_id")
        values["status"] = session.status.value
        try:
            if previous is None:
                self._session.add(Row(**values))
                self._session.flush()
            else:
                changed = self._session.execute(
                    update(Row)
                    .where(Row.id == session.session_id, Row.row_version == previous.row_version)
                    .values(**values)
                    .returning(Row.id)
                ).scalar_one_or_none()
                if changed is None:
                    raise ConflictError(
                        "editor_concurrent_change", "Editor session changed concurrently"
                    )
        except IntegrityError as error:
            raise ConflictError(
                "editor_concurrent_change", "Editor session changed concurrently"
            ) from error

    def pending(self, terminal_id: UUID, limit: int) -> tuple[EditorSession, ...]:
        rows = (
            self._session.execute(
                select(Row)
                .where(Row.terminal_id == terminal_id, Row.status.in_(ACTIVE))
                .order_by(Row.created_at, Row.id)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return tuple(record(row) for row in rows)
