from __future__ import annotations

from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.execution_repositories import (
    PostgresCapabilityProfileRepository,
    PostgresExecutionLeaseRepository,
    PostgresRegistrationGrantRepository,
    PostgresTargetDeviceRepository,
    PostgresTargetIdentifierRepository,
    PostgresTerminalCredentialRepository,
    PostgresTerminalRepository,
)
from al1s.adapters.postgres.repositories import PostgresAuditRepository, PostgresOutboxRepository


class ExecutionSqlAlchemyUnitOfWork:
    """One transaction for Stage 3 execution resources and Stage 2 audit/outbox."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    def __enter__(self) -> ExecutionSqlAlchemyUnitOfWork:
        if self._session is not None:
            raise RuntimeError("unit of work cannot be entered twice")
        session = self._session_factory()
        self._session = session
        self.grants = PostgresRegistrationGrantRepository(session)
        self.terminals = PostgresTerminalRepository(session)
        self.credentials = PostgresTerminalCredentialRepository(session)
        self.capabilities = PostgresCapabilityProfileRepository(session)
        self.target_devices = PostgresTargetDeviceRepository(session)
        self.target_identifiers = PostgresTargetIdentifierRepository(session)
        self.leases = PostgresExecutionLeaseRepository(session)
        self.outbox = PostgresOutboxRepository(session, owner_module="maa")
        self.audit = PostgresAuditRepository(session, owner_module="maa")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._require_session()
        try:
            if exc_type is not None or session.in_transaction():
                session.rollback()
        finally:
            session.close()
            self._session = None

    def commit(self) -> None:
        self._require_session().commit()

    def rollback(self) -> None:
        self._require_session().rollback()

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        return self._session
