from __future__ import annotations

from al1s.adapters.postgres.component_guard import ComponentAdmission
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
from al1s.adapters.postgres.transaction_scope import ExplicitCommitUnitOfWork


class SchedulingSqlAlchemyUnitOfWork(ExplicitCommitUnitOfWork):
    """One transaction for Stage 3B scheduling, snapshots, outbox, and audit."""

    def __enter__(self) -> SchedulingSqlAlchemyUnitOfWork:
        if self._session is not None:
            raise RuntimeError("unit of work cannot be entered twice")
        session = self._session_factory()
        self._session = session
        self.component_admission = ComponentAdmission(session)
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

    def flush(self) -> None:
        self._require_session().flush()
