from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID, uuid4

from al1s.execution.editor_sessions import (
    EditorSession,
    EditorStatus,
    expire_pending,
    open_connection,
    report_session,
    request_close,
)
from al1s.execution.errors import ConflictError, InvalidRequestError, NotFoundError
from al1s.secrets.security import FernetSecretCipher


class EditorTransaction(Protocol):
    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        kind: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
    def target_terminal(self, device_id: UUID, *, lock: bool = False) -> UUID: ...
    def by_request(self, request_id: UUID) -> EditorSession | None: ...
    def by_device(self, device_id: UUID) -> EditorSession | None: ...
    def get(self, session_id: UUID) -> EditorSession: ...
    def put(self, session: EditorSession, previous: EditorSession | None) -> None: ...
    def pending(self, terminal_id: UUID, limit: int) -> tuple[EditorSession, ...]: ...


class EditorSessionService:
    def __init__(
        self,
        transactions: Callable[[], EditorTransaction],
        cipher: FernetSecretCipher,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._transactions, self._cipher, self._clock = transactions, cipher, clock

    def create(self, device_id: UUID, request_id: UUID) -> EditorSession:
        with self._transactions() as tx:
            terminal_id = tx.target_terminal(device_id, lock=True)
            if existing := tx.by_request(request_id):
                if existing.device_id != device_id:
                    raise ConflictError(
                        "editor_idempotency_conflict", "Request belongs to another device"
                    )
                return existing
            now = self._clock()
            if active := tx.by_device(device_id):
                expired = expire_pending(active, now)
                if expired == active:
                    raise ConflictError(
                        "editor_device_busy", "Device already has an editor session"
                    )
                tx.put(expired, active)
            session = EditorSession(
                uuid4(),
                terminal_id,
                device_id,
                request_id,
                EditorStatus.PENDING,
                now,
                now + timedelta(seconds=30),
                now,
            )
            tx.put(session, None)
            return session

    def current(self, device_id: UUID) -> EditorSession | None:
        """Find only the device's editor lease; never cancel a formal task."""
        with self._transactions() as tx:
            terminal_id = tx.target_terminal(device_id)
            session = tx.by_device(device_id)
            if session is None or session.terminal_id != terminal_id:
                return None
            current = expire_pending(session, self._clock())
            tx.put(current, session)
            return current if current.status in {
                EditorStatus.PENDING, EditorStatus.ACTIVE, EditorStatus.CLOSING,
            } else None

    def detail(self, session_id: UUID) -> tuple[EditorSession, dict[str, str] | None]:
        with self._transactions() as tx:
            session = tx.get(session_id)
            if tx.target_terminal(session.device_id) != session.terminal_id:
                raise NotFoundError("editor_session")
            current = expire_pending(session, self._clock())
            tx.put(current, session)
            return current, open_connection(current, self._cipher)

    def close(self, session_id: UUID) -> EditorSession:
        with self._transactions() as tx:
            session = tx.get(session_id)
            current = request_close(session, self._clock())
            tx.put(current, session)
            return current

    def list_terminal(self, terminal_id: UUID, limit: int = 20) -> tuple[EditorSession, ...]:
        if not 1 <= limit <= 50:
            raise InvalidRequestError("invalid_editor_limit", "Limit must be 1 to 50")
        with self._transactions() as tx:
            return tx.pending(terminal_id, limit)

    def report(
        self,
        session_id: UUID,
        terminal_id: UUID,
        instance_id: UUID,
        status: EditorStatus,
        connection: dict[str, str] | None,
    ) -> EditorSession:
        with self._transactions() as tx:
            session = tx.get(session_id)
            if session.terminal_id != terminal_id:
                raise NotFoundError("editor_session")
            if (
                status is EditorStatus.ACTIVE
                and tx.target_terminal(session.device_id) != terminal_id
            ):
                raise NotFoundError("editor_session")
            current = report_session(
                session, terminal_id, instance_id, status, self._clock(), self._cipher, connection
            )
            tx.put(current, session)
            return current
