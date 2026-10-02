from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol
from uuid import UUID

from al1s.execution.scheduling_ports import ContentScheduleImpactRepository
from al1s.execution.scheduling_types import SnapshotBlobReference
from al1s.kernel.ports import (
    AuditRepository,
    BlobCatalogRepository,
    BlobStore,
    GcJobRepository,
    OutboxRepository,
)
from al1s.maa.types import (
    ApplicationDeviceRecord,
    ApplicationRecord,
    ImportBatchRecord,
    ImportItemRecord,
    MutationReceiptRecord,
    NewImportItem,
    QualificationKind,
    QuickTestEventRecord,
    QuickTestSessionRecord,
    ScriptBlobReference,
    ScriptQualificationReceipt,
    ScriptRecord,
    ScriptType,
    ScriptVersionRecord,
    StrategyModuleRecord,
    StrategyRecord,
    StrategyVersionRecord,
)


class MaaApplicationRepository(Protocol):
    def find_import_targets(
        self, package_names: Sequence[str]
    ) -> dict[str, list[ApplicationRecord]]: ...

    def get_active(
        self, application_id: UUID, *, for_update: bool = False
    ) -> ApplicationRecord | None: ...

    def find_active_by_packages(
        self, package_names: Sequence[str]
    ) -> dict[str, ApplicationRecord]: ...

    def add_many(self, applications: Sequence[ApplicationRecord]) -> None: ...

    def update_display_name(
        self,
        application_id: UUID,
        expected_version: int,
        display_name: str,
        now: datetime,
    ) -> ApplicationRecord | None: ...

    def update_icon(
        self,
        application_id: UUID,
        expected_version: int,
        icon_png: bytes | None,
        now: datetime,
    ) -> ApplicationRecord | None: ...

    def soft_delete(self, application_id: UUID, expected_version: int, now: datetime) -> bool: ...

    def list_active(self, *, after_id: UUID | None, limit: int) -> list[ApplicationRecord]: ...


class MaaApplicationDeletionRepository(Protocol):
    def counts(self, application_id: UUID) -> tuple[int, int, int]: ...

    def active_tasks(self, application_id: UUID, limit: int = 51) -> list[UUID]: ...

    def lock_contents(self, application_id: UUID) -> None: ...

    def has_external_references(self, application_id: UUID) -> bool: ...

    def soft_delete_contents(self, application_id: UUID, now: datetime) -> None: ...


class MaaApplicationDeviceRepository(Protocol):
    def lock_active_device(self, device_id: UUID) -> bool: ...

    def for_phone_package(
        self, device_id: UUID, package_name: str
    ) -> ApplicationDeviceRecord | None: ...

    def for_application(self, application_id: UUID) -> list[ApplicationDeviceRecord]: ...

    def for_device(self, device_id: UUID, limit: int = 201) -> list[ApplicationDeviceRecord]: ...

    def add(self, binding: ApplicationDeviceRecord) -> None: ...

    def remove(self, application_id: UUID, device_id: UUID, now: datetime) -> bool: ...

    def active_maa_tasks(self, device_id: UUID, limit: int = 51) -> list[UUID]: ...

    def active_content_tasks(
        self, logical_content_ids: Sequence[str], limit: int = 51
    ) -> list[UUID]: ...


class MaaScriptRepository(Protocol):
    def list_editor_drafts(
        self, *, device_id: UUID, after_id: UUID | None, limit: int
    ) -> list[ScriptRecord]: ...

    def list_saved_for_library(
        self, *, application_id: UUID, after_id: UUID | None, limit: int
    ) -> list[ScriptRecord]: ...

    def move_display_order(
        self,
        *,
        application_id: UUID,
        script_id: UUID,
        target_id: UUID,
        placement: str,
    ) -> bool: ...

    def count_saved_by_applications(self, application_ids: Sequence[UUID]) -> dict[UUID, int]: ...

    def find_import_boundaries(
        self, application_ids: Sequence[UUID]
    ) -> dict[tuple[UUID, ScriptType], ScriptRecord]: ...

    def replace_imported_versions(
        self, updates: Sequence[tuple[UUID, int, UUID]], now: datetime
    ) -> int: ...

    def list_referrers(
        self, script_id: UUID, *, after_id: UUID | None, limit: int
    ) -> list[dict[str, object]]: ...

    def has_strategy_referrers(self, script_id: UUID) -> bool: ...

    def soft_delete(self, script_id: UUID, expected_version: int, now: datetime) -> bool: ...

    def update_name(
        self,
        script_id: UUID,
        expected_version: int,
        name: str,
        normalized_name: str,
        now: datetime,
    ) -> ScriptRecord | None: ...

    def get_active(self, script_id: UUID, *, for_update: bool = False) -> ScriptRecord | None: ...

    def find_active_by_keys(
        self,
        keys: Sequence[tuple[UUID, str]],
        *,
        for_update: bool = False,
    ) -> dict[tuple[UUID, str], ScriptRecord]: ...

    def find_active_by_ids(self, script_ids: Sequence[UUID]) -> dict[UUID, ScriptRecord]: ...

    def find_active_by_types(
        self, application_id: UUID, script_types: Sequence[ScriptType]
    ) -> dict[ScriptType, ScriptRecord]: ...

    def add_many(self, scripts: Sequence[ScriptRecord]) -> None: ...

    def set_initial_candidates(
        self, versions_by_script: dict[UUID, UUID], now: datetime
    ) -> int: ...

    def activate_imported_versions(
        self, versions_by_script: dict[UUID, UUID], now: datetime
    ) -> int: ...

    def set_candidate(
        self,
        script_id: UUID,
        expected_version: int,
        candidate_version_id: UUID,
        now: datetime,
    ) -> ScriptRecord | None: ...

    def activate_saved_version(
        self,
        script_id: UUID,
        expected_version: int,
        saved_version_id: UUID,
        now: datetime,
    ) -> ScriptRecord | None: ...

    def publish_candidate(
        self,
        script_id: UUID,
        expected_version: int,
        candidate_version_id: UUID,
        now: datetime,
    ) -> ScriptRecord | None: ...

    def move_to_application(
        self,
        script_id: UUID,
        expected_version: int,
        application_id: UUID,
        now: datetime,
    ) -> ScriptRecord | None: ...

    def list_active(
        self,
        *,
        application_id: UUID | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[ScriptRecord]: ...


class MaaScriptVersionRepository(Protocol):
    def next_import_revisions(self, script_ids: Sequence[UUID]) -> dict[UUID, int]: ...

    def script_ids_for_versions(self, version_ids: Sequence[UUID]) -> dict[UUID, UUID]: ...

    def get(self, script_version_id: UUID) -> ScriptVersionRecord | None: ...

    def find_by_ids(
        self, script_version_ids: Sequence[UUID]
    ) -> dict[UUID, ScriptVersionRecord]: ...

    def find_by_hash(self, script_id: UUID, manifest_hash: str) -> ScriptVersionRecord | None: ...

    def next_revision(self, script_id: UUID) -> int: ...

    def add_many(self, versions: Sequence[ScriptVersionRecord]) -> None: ...

    def add_blob_references(self, references: Sequence[ScriptBlobReference]) -> None: ...

    def list_blob_references(self, script_version_id: UUID) -> list[ScriptBlobReference]: ...

    def has_blob_reference(self, script_version_id: UUID, blob_id: UUID) -> bool: ...

    def list_blob_references_many(
        self, script_version_ids: Sequence[UUID]
    ) -> dict[UUID, list[ScriptBlobReference]]: ...

    def list_for_script(
        self,
        script_id: UUID,
        *,
        after_revision: int | None,
        limit: int,
        newest_first: bool = False,
    ) -> list[ScriptVersionRecord]: ...


class MaaQualificationRepository(Protocol):
    def get(self, receipt_id: UUID) -> ScriptQualificationReceipt | None: ...

    def add(self, receipt: ScriptQualificationReceipt) -> None: ...

    def add_many(self, receipts: Sequence[ScriptQualificationReceipt]) -> None: ...

    def find_by_idempotency(
        self,
        script_version_id: UUID,
        kind: QualificationKind,
        idempotency_key: str,
    ) -> ScriptQualificationReceipt | None: ...

    def has_passed(
        self,
        script_version_id: UUID,
        manifest_hash: str,
        kinds: Sequence[QualificationKind],
        *,
        static_executor_version: str,
    ) -> set[QualificationKind]: ...


class MaaQuickTestSessionRepository(Protocol):
    def add(
        self,
        session: QuickTestSessionRecord,
        blobs: Sequence[SnapshotBlobReference],
    ) -> None: ...

    def get(
        self, session_id: UUID, *, for_update: bool = False
    ) -> QuickTestSessionRecord | None: ...

    def find_by_idempotency(
        self, script_version_id: UUID, idempotency_key: str
    ) -> QuickTestSessionRecord | None: ...

    def list_pending(
        self, terminal_id: UUID, now: datetime, *, limit: int
    ) -> list[QuickTestSessionRecord]: ...

    def claim(
        self,
        session_id: UUID,
        expected_version: int,
        terminal_id: UUID,
        claimed_at: datetime,
    ) -> QuickTestSessionRecord | None: ...

    def complete(
        self,
        session_id: UUID,
        expected_version: int,
        qualification_receipt_id: UUID,
        completed_at: datetime,
    ) -> QuickTestSessionRecord | None: ...

    def expire(
        self, session_id: UUID, expected_version: int, expired_at: datetime
    ) -> QuickTestSessionRecord | None: ...

    def request_cancel(
        self, session_id: UUID, expected_version: int, now: datetime
    ) -> QuickTestSessionRecord | None: ...

    def append_events(
        self, session_id: UUID, events: Sequence[QuickTestEventRecord], cutoff: datetime
    ) -> int: ...

    def list_events(
        self, session_id: UUID, *, after: int, limit: int
    ) -> list[QuickTestEventRecord]: ...


class MaaStrategyRepository(Protocol):
    def get_active(
        self, strategy_id: UUID, *, for_update: bool = False
    ) -> StrategyRecord | None: ...

    def add(self, strategy: StrategyRecord) -> None: ...

    def update_name(
        self,
        strategy_id: UUID,
        expected_version: int,
        name: str,
        normalized_name: str,
        now: datetime,
    ) -> StrategyRecord | None: ...

    def soft_delete(self, strategy_id: UUID, expected_version: int, now: datetime) -> bool: ...

    def list_current_using_script_version(
        self, script_version_id: UUID, *, for_update: bool = False
    ) -> list[StrategyRecord]: ...

    def set_current_version(
        self,
        strategy_id: UUID,
        expected_version: int,
        strategy_version_id: UUID,
        now: datetime,
    ) -> StrategyRecord | None: ...

    def set_current_versions(
        self,
        updates: Sequence[tuple[UUID, int, UUID]],
        now: datetime,
    ) -> dict[UUID, StrategyRecord]: ...

    def list_active(
        self,
        *,
        application_id: UUID | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[StrategyRecord]: ...


class MaaStrategyVersionRepository(Protocol):
    def get(self, strategy_version_id: UUID) -> StrategyVersionRecord | None: ...

    def find_by_ids(
        self, strategy_version_ids: Sequence[UUID]
    ) -> dict[UUID, StrategyVersionRecord]: ...

    def find_by_hash(
        self, strategy_id: UUID, manifest_hash: str
    ) -> StrategyVersionRecord | None: ...

    def find_by_hashes(
        self, keys: Sequence[tuple[UUID, str]]
    ) -> dict[tuple[UUID, str], StrategyVersionRecord]: ...

    def next_revision(self, strategy_id: UUID) -> int: ...

    def next_revisions(self, strategy_ids: Sequence[UUID]) -> dict[UUID, int]: ...

    def add(self, version: StrategyVersionRecord) -> None: ...

    def add_many(self, versions: Sequence[StrategyVersionRecord]) -> None: ...

    def add_modules(self, modules: Sequence[StrategyModuleRecord]) -> None: ...

    def list_modules(self, strategy_version_id: UUID) -> list[StrategyModuleRecord]: ...

    def list_modules_many(
        self, strategy_version_ids: Sequence[UUID]
    ) -> dict[UUID, list[StrategyModuleRecord]]: ...

    def list_for_strategy(
        self, strategy_id: UUID, *, after_revision: int | None, limit: int
    ) -> list[StrategyVersionRecord]: ...


class MaaImportRepository(Protocol):
    def acquire_finalize_lock(self) -> None: ...

    def find_by_logical_hash(
        self, logical_sha256: str, *, for_update: bool = False
    ) -> ImportBatchRecord | None: ...

    def add(self, batch: ImportBatchRecord) -> None: ...

    def restart_failed(
        self,
        batch_id: UUID,
        expected_version: int,
        archive_sha256: str,
        now: datetime,
    ) -> bool: ...

    def complete(
        self,
        batch_id: UUID,
        expected_version: int,
        completed_at: datetime,
        *,
        strategy_id: UUID | None = None,
    ) -> bool: ...

    def fail(
        self,
        batch_id: UUID,
        expected_version: int,
        error_code: str,
        diagnostic: str,
        completed_at: datetime,
    ) -> bool: ...

    def add_items(self, items: Sequence[NewImportItem], created_at: datetime) -> None: ...

    def get(self, batch_id: UUID) -> ImportBatchRecord | None: ...

    def list_items(
        self,
        batch_id: UUID,
        *,
        after_ordinal: int | None,
        limit: int,
    ) -> list[ImportItemRecord]: ...


class MaaMutationReceiptRepository(Protocol):
    def acquire_idempotency_lock(self, operation: str, idempotency_key: str) -> None: ...

    def get_by_idempotency(
        self, operation: str, idempotency_key: str
    ) -> MutationReceiptRecord | None: ...

    def add(self, receipt: MutationReceiptRecord) -> None: ...


class MaaUnitOfWork(Protocol):
    applications: MaaApplicationRepository
    application_deletions: MaaApplicationDeletionRepository
    application_devices: MaaApplicationDeviceRepository
    scripts: MaaScriptRepository
    script_versions: MaaScriptVersionRepository
    qualifications: MaaQualificationRepository
    quick_tests: MaaQuickTestSessionRepository
    strategies: MaaStrategyRepository
    strategy_versions: MaaStrategyVersionRepository
    schedule_impacts: ContentScheduleImpactRepository
    imports: MaaImportRepository
    mutation_receipts: MaaMutationReceiptRepository
    blobs: BlobCatalogRepository
    gc_jobs: GcJobRepository
    outbox: OutboxRepository
    audit: AuditRepository

    def __enter__(self) -> MaaUnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def flush(self) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


__all__ = ["BlobStore", "MaaUnitOfWork"]
