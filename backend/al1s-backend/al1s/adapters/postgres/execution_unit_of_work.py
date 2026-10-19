from __future__ import annotations

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
from al1s.adapters.postgres.transaction_scope import ExplicitCommitUnitOfWork


class ExecutionSqlAlchemyUnitOfWork(ExplicitCommitUnitOfWork):
    """One transaction for Stage 3 execution resources and Stage 2 audit/outbox."""

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
