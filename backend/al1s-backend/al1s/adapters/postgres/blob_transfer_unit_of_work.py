from __future__ import annotations

from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.blob_transfer_repository import (
    PostgresTerminalBlobAccessRepository,
)


class TerminalBlobSqlAlchemyUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    def __enter__(self) -> TerminalBlobSqlAlchemyUnitOfWork:
        if self._session is not None:
            raise RuntimeError("unit of work cannot be entered twice")
        self._session = self._session_factory()
        self.blob_access = PostgresTerminalBlobAccessRepository(self._session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        try:
            self._session.rollback()
        finally:
            self._session.close()
            self._session = None
