from __future__ import annotations

from al1s.adapters.postgres.component_guard import ComponentAdmission
from al1s.adapters.postgres.maa_applicability_repository import (
    PostgresMaaApplicationDeviceRepository,
)
from al1s.adapters.postgres.maa_application_deletion_repository import (
    PostgresMaaApplicationDeletionRepository,
)
from al1s.adapters.postgres.maa_repositories import (
    PostgresMaaApplicationRepository,
    PostgresMaaImportRepository,
    PostgresMaaMutationReceiptRepository,
    PostgresMaaQualificationRepository,
    PostgresMaaQuickTestSessionRepository,
    PostgresMaaScriptRepository,
    PostgresMaaScriptVersionRepository,
    PostgresMaaStrategyRepository,
    PostgresMaaStrategyVersionRepository,
)
from al1s.adapters.postgres.repositories import (
    PostgresAuditRepository,
    PostgresBlobCatalogRepository,
    PostgresGcJobRepository,
    PostgresOutboxRepository,
)
from al1s.adapters.postgres.scheduling_repositories import (
    PostgresContentScheduleImpactRepository,
)
from al1s.adapters.postgres.transaction_scope import ExplicitCommitUnitOfWork


class MaaSqlAlchemyUnitOfWork(ExplicitCommitUnitOfWork):
    """One transaction for Maa assets plus shared Blob, audit, and outbox facts."""

    def __enter__(self) -> MaaSqlAlchemyUnitOfWork:
        if self._session is not None:
            raise RuntimeError("unit of work cannot be entered twice")
        session = self._session_factory()
        self._session = session
        self.component_admission = ComponentAdmission(session)
        self.applications = PostgresMaaApplicationRepository(session)
        self.application_deletions = PostgresMaaApplicationDeletionRepository(session)
        self.application_devices = PostgresMaaApplicationDeviceRepository(session)
        self.scripts = PostgresMaaScriptRepository(session)
        self.script_versions = PostgresMaaScriptVersionRepository(session)
        self.qualifications = PostgresMaaQualificationRepository(session)
        self.quick_tests = PostgresMaaQuickTestSessionRepository(session)
        self.strategies = PostgresMaaStrategyRepository(session)
        self.strategy_versions = PostgresMaaStrategyVersionRepository(session)
        self.schedule_impacts = PostgresContentScheduleImpactRepository(session)
        self.imports = PostgresMaaImportRepository(session)
        self.mutation_receipts = PostgresMaaMutationReceiptRepository(session)
        self.blobs = PostgresBlobCatalogRepository(session)
        self.gc_jobs = PostgresGcJobRepository(session)
        self.outbox = PostgresOutboxRepository(session, owner_module="maa")
        self.audit = PostgresAuditRepository(session, owner_module="maa")
        return self

    def flush(self) -> None:
        self._require_session().flush()
