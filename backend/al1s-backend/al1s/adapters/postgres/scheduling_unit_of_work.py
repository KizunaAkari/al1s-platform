from __future__ import annotations

from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.delivery_repositories import (
    PostgresCommandAcknowledgementRepository,
    PostgresOfflineStartPermitRepository,
    PostgresTaskPackageRepository,
    PostgresTerminalCommandRepository,
    PostgresTerminalReportRepository,
)
from al1s.adapters.postgres.execution_repositories import PostgresExecutionLeaseRepository
from al1s.adapters.postgres.repositories import PostgresAuditRepository, PostgresOutboxRepository
from al1s.adapters.postgres.scheduling_repositories import (
    PostgresAttemptRepository,
    PostgresExecutionRepository,
    PostgresOccurrenceRepository,
    PostgresScheduleRepository,
    PostgresScheduleRevisionRepository,
    PostgresSnapshotRepository,
    PostgresTaskRepository,
    PostgresTaskRetryOriginRepository,
    PostgresTransitionRepository,
)


class SchedulingSqlAlchemyUnitOfWork:
    """One transaction for Stage 3B scheduling, snapshots, outbox, and audit."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    def __enter__(self) -> SchedulingSqlAlchemyUnitOfWork:
        if self._session is not None:
            raise RuntimeError("unit of work cannot be entered twice")
        session = self._session_factory()
        self._session = session
        self.tasks = PostgresTaskRepository(session)
        self.schedules = PostgresScheduleRepository(session)
        self.schedule_revisions = PostgresScheduleRevisionRepository(session)
        self.occurrences = PostgresOccurrenceRepository(session)
        self.executions = PostgresExecutionRepository(session)
        self.attempts = PostgresAttemptRepository(session)
        self.snapshots = PostgresSnapshotRepository(session)
        self.retry_origins = PostgresTaskRetryOriginRepository(session)
        self.transitions = PostgresTransitionRepository(session)
        self.packages = PostgresTaskPackageRepository(session)
        self.commands = PostgresTerminalCommandRepository(session)
        self.terminal_reports = PostgresTerminalReportRepository(session)
        self.command_acknowledgements = PostgresCommandAcknowledgementRepository(session)
        self.offline_permits = PostgresOfflineStartPermitRepository(session)
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

    def flush(self) -> None:
        self._require_session().flush()

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        return self._session
