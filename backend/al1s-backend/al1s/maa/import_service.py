from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from al1s.execution.definitions import canonical_manifest_hash
from al1s.kernel.types import BlobRecord, NewAuditEntry, NewBlob, NewOutboxEvent
from al1s.maa.archive import (
    ArchiveScript,
    ParsedScriptArchive,
    parse_script_archive,
    replace_resource_pointer,
)
from al1s.maa.errors import MaaDomainError
from al1s.maa.import_activation import validate_import_closure
from al1s.maa.import_persistence import persist_imported_scripts
from al1s.maa.import_selection import resolve_import
from al1s.maa.import_strategy import activate_strategy
from al1s.maa.naming import normalize_asset_name
from al1s.maa.ports import BlobStore, MaaUnitOfWork
from al1s.maa.strategy_service import MaaStrategyService
from al1s.maa.types import (
    ApplicationRecord,
    ImportBatchRecord,
    ImportItemStatus,
    ImportResult,
    ImportStatus,
    NewImportItem,
    QualificationKind,
    QualificationStatus,
    ScriptBlobReference,
    ScriptQualificationReceipt,
    ScriptRecord,
    ScriptStatus,
    ScriptType,
    ScriptVersionRecord,
)
from al1s.maa.validation import (
    VALIDATOR_VERSION,
    validate_script_document,
)

MaaUowFactory = Callable[[], MaaUnitOfWork]
IMPORT_PROCESSING_LIMIT = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class _PreparedScript:
    archived: ArchiveScript
    application_package: str
    document: dict[str, object]
    script_type: ScriptType
    migration_codes: tuple[str, ...]


class MaaArchiveImportService:
    def __init__(
        self,
        uow_factory: MaaUowFactory,
        blob_store: BlobStore,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._blob_store = blob_store
        self._now = now or (lambda: datetime.now(UTC))

    def import_archive(
        self, content: bytes, *, correlation_id: UUID,
        target_applications: dict[str, UUID] | None = None,
        confirmed_overwrites: dict[UUID, int] | None = None,
    ) -> ImportResult:
        parsed, batch, replayed = self.prepare_archive(
            content, target_applications=target_applications,
            confirmed_overwrites=confirmed_overwrites,
        )
        if replayed:
            return ImportResult(batch=batch, replayed=True)
        return self.finish_archive(parsed, batch, correlation_id=correlation_id)

    def prepare_archive(
        self, content: bytes, *,
        target_applications: dict[str, UUID] | None = None,
        confirmed_overwrites: dict[UUID, int] | None = None,
    ) -> tuple[ParsedScriptArchive, ImportBatchRecord, bool]:
        parsed = parse_script_archive(content)
        options = {
            "contract": "confirmed-import-v1", "archive": parsed.logical_sha256,
            "applications": {k: str(v) for k, v in (target_applications or {}).items()},
            "overwrites": {str(k): v for k, v in (confirmed_overwrites or {}).items()},
        }
        parsed = replace(
            parsed,
            logical_sha256=hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest(),
            target_applications=target_applications or {},
            confirmed_overwrites=confirmed_overwrites or {},
        )
        self._prepare_scripts(parsed)
        batch, replayed = self._start_batch(parsed)
        return parsed, batch, replayed

    def finish_archive(
        self, parsed: ParsedScriptArchive, batch: ImportBatchRecord, *, correlation_id: UUID,
    ) -> ImportResult:
        pending: dict[str, NewBlob] = {}
        try:
            with self._uow_factory() as uow:
                current = uow.imports.get(batch.batch_id)
                if (
                    current is None or current.status is not ImportStatus.PROCESSING
                    or current.row_version != batch.row_version
                ):
                    raise MaaDomainError(
                        "archive_import_state_conflict", "Import is no longer processing", 409,
                    )
                if self._now() >= current.created_at + IMPORT_PROCESSING_LIMIT:
                    raise MaaDomainError(
                        "archive_import_timeout", "Import exceeded its processing deadline", 409,
                    )
            prepared_scripts = self._prepare_scripts(parsed)
            pending = self._prepare_missing_blobs(parsed)
            self._upload_blobs(parsed, pending)
            completed = self._finalize_import(
                parsed,
                prepared_scripts,
                pending,
                correlation_id,
                batch.row_version,
            )
            return ImportResult(batch=completed, replayed=False)
        except Exception as exc:
            self._record_failure(parsed, pending, correlation_id, exc, batch.row_version)
            if isinstance(exc, MaaDomainError):
                raise
            raise MaaDomainError(
                "archive_import_failed",
                "Archive import failed before any script was published",
                500,
            ) from exc

    def cancel(self, batch_id: UUID, expected_version: int) -> ImportBatchRecord:
        # Same lock as publication: cancellation never races a committed import.
        with self._uow_factory() as uow:
            uow.imports.acquire_finalize_lock()
            batch = uow.imports.get(batch_id)
            if batch is None:
                raise MaaDomainError("import_batch_not_found", "Import batch not found", 404)
            if batch.error_code == "archive_import_cancelled":
                return batch
            if batch.status is not ImportStatus.PROCESSING or batch.row_version != expected_version:
                raise MaaDomainError(
                    "import_not_cancellable", "Import already ended or changed", 409,
                )
            now = self._now()
            if not uow.imports.fail(
                batch_id, expected_version, "archive_import_cancelled", "Cancelled by user", now,
            ):
                raise MaaDomainError("import_not_cancellable", "Import already changed", 409)
            uow.audit.add(NewAuditEntry(
                audit_id=uuid4(), actor_type="user", actor_id=None,
                action="maa.archive_import.cancelled", target_type="maa_import_batch",
                target_id=batch_id, correlation_id=batch_id,
                details={"batch_id": str(batch_id)}, summary="Maa archive import cancelled",
            ))
            uow.commit()
            return replace(
                batch, status=ImportStatus.FAILED, error_code="archive_import_cancelled",
                diagnostic="Cancelled by user", completed_at=now, row_version=expected_version + 1,
            )

    def _start_batch(self, parsed: ParsedScriptArchive) -> tuple[ImportBatchRecord, bool]:
        now = self._now()
        with self._uow_factory() as uow:
            uow.imports.acquire_finalize_lock()
            existing = uow.imports.find_by_logical_hash(parsed.logical_sha256, for_update=True)
            if existing is not None:
                if existing.status is ImportStatus.COMPLETED:
                    retained = self._retained_scripts(uow, existing.batch_id, len(parsed.scripts))
                    strategy_present = parsed.strategy is None or (
                        existing.strategy_id is not None
                        and uow.strategies.get_active(existing.strategy_id) is not None
                    )
                    if len(retained) == len(parsed.scripts) and strategy_present:
                        return existing, True
                if existing.status is ImportStatus.PROCESSING:
                    if existing.created_at + IMPORT_PROCESSING_LIMIT > now:
                        raise MaaDomainError(
                            "archive_import_in_progress",
                            "The same logical archive is already being imported",
                            409,
                            context={"batch_id": str(existing.batch_id)},
                        )
                    if not uow.imports.fail(
                        existing.batch_id,
                        existing.row_version,
                        "archive_import_interrupted",
                        "Previous import expired",
                        now,
                    ):
                        raise MaaDomainError(
                            "archive_import_version_conflict", "Import changed", 409
                        )
                    existing = replace(
                        existing, status=ImportStatus.FAILED, row_version=existing.row_version + 1
                    )
                resolve_import(uow, parsed)
                if not uow.imports.restart_failed(
                    existing.batch_id,
                    existing.row_version,
                    parsed.archive_sha256,
                    now,
                ):
                    raise MaaDomainError(
                        "archive_import_version_conflict",
                        "Archive import state changed concurrently",
                        409,
                    )
                restarted = ImportBatchRecord(
                    batch_id=existing.batch_id,
                    logical_sha256=existing.logical_sha256,
                    archive_sha256=parsed.archive_sha256,
                    archive_schema=existing.archive_schema,
                    status=ImportStatus.PROCESSING,
                    script_count=existing.script_count,
                    application_count=existing.application_count,
                    resource_reference_count=existing.resource_reference_count,
                    unique_resource_count=existing.unique_resource_count,
                    error_code=None,
                    diagnostic=None,
                    created_at=now,
                    completed_at=None,
                    row_version=existing.row_version + 1,
                    strategy_id=existing.strategy_id,
                )
                uow.commit()
                return restarted, False

            resolve_import(uow, parsed)
            batch = ImportBatchRecord(
                batch_id=uuid4(),
                logical_sha256=parsed.logical_sha256,
                archive_sha256=parsed.archive_sha256,
                archive_schema=parsed.schema,
                status=ImportStatus.PROCESSING,
                script_count=len(parsed.scripts),
                application_count=len(parsed.categories),
                resource_reference_count=parsed.resource_reference_count,
                unique_resource_count=len(parsed.resources),
                error_code=None,
                diagnostic=None,
                created_at=now,
                completed_at=None,
                row_version=1,
            )
            uow.imports.add(batch)
            uow.commit()
            return batch, False

    @staticmethod
    def _retained_scripts(
        uow: MaaUnitOfWork, batch_id: UUID, count: int
    ) -> dict[int, ScriptRecord]:
        items = uow.imports.list_items(batch_id, after_ordinal=None, limit=count)
        active = uow.scripts.find_active_by_ids(
            tuple(item.script_id for item in items if item.script_id)
        )
        return {
            item.archive_ordinal: active[item.script_id]
            for item in items
            if item.script_id in active
        }

    def _prepare_missing_blobs(self, parsed: ParsedScriptArchive) -> dict[str, NewBlob]:
        with self._uow_factory() as uow:
            ready = uow.blobs.find_ready_by_sha256_many(tuple(parsed.resources))
            pending = {
                digest: NewBlob(
                    blob_id=uuid4(),
                    sha256=digest,
                    size_bytes=resource.size_bytes,
                    media_type=resource.media_type,
                    object_key=f"maa/resources/sha256/{digest}/{uuid4()}",
                )
                for digest, resource in parsed.resources.items()
                if digest not in ready
            }
            for blob in pending.values():
                uow.blobs.add_pending(blob)
            uow.flush()
            jobs = [(uuid4(), blob.blob_id) for blob in pending.values()]
            if uow.gc_jobs.schedule_many(jobs, self._now() + timedelta(hours=24)) != len(jobs):
                raise MaaDomainError(
                    "archive_staging_failed", "Could not reserve resource cleanup", 409
                )
            uow.commit()
            return pending

    def _upload_blobs(self, parsed: ParsedScriptArchive, pending: dict[str, NewBlob]) -> None:
        for digest, blob in pending.items():
            resource = parsed.resources[digest]
            self._blob_store.put(blob.object_key, resource.body, resource.media_type)
            head = self._blob_store.head(blob.object_key)
            actual_media_type = head.media_type.split(";", 1)[0].strip().lower()
            if (
                head.size_bytes != resource.size_bytes
                or actual_media_type != resource.media_type.lower()
            ):
                raise MaaDomainError(
                    "blob_upload_verification_failed",
                    "Uploaded script resource metadata does not match the archive",
                    502,
                )

    def _finalize_import(
        self,
        parsed: ParsedScriptArchive,
        prepared_scripts: tuple[_PreparedScript, ...],
        pending: dict[str, NewBlob],
        correlation_id: UUID,
        expected_batch_version: int,
    ) -> ImportBatchRecord:
        now = self._now()
        with self._uow_factory() as uow:
            uow.imports.acquire_finalize_lock()
            batch = uow.imports.find_by_logical_hash(parsed.logical_sha256, for_update=True)
            if (
                batch is None
                or batch.status is not ImportStatus.PROCESSING
                or batch.row_version != expected_batch_version
            ):
                raise MaaDomainError(
                    "archive_import_state_conflict", "Archive import is no longer processing", 409
                )
            if now >= batch.created_at + IMPORT_PROCESSING_LIMIT:
                raise MaaDomainError(
                    "archive_import_timeout", "Import exceeded its processing deadline", 409
                )

            ready = self._finalize_blobs(uow, parsed, pending, now)

            selection = resolve_import(uow, parsed)
            applications = selection.applications
            replacements = selection.replacements
            retained = self._retained_scripts(uow, batch.batch_id, len(parsed.scripts))
            revisions = uow.script_versions.next_import_revisions(
                [s.script_id for s in replacements.values()]
            )

            scripts, versions, references, qualifications, items = self._build_script_facts(
                prepared_scripts,
                applications,
                ready,
                batch.batch_id,
                correlation_id,
                now,
                retained,
                replacements,
                revisions,
            )
            retained_versions = uow.script_versions.find_by_ids(
                [
                    script.current_version_id
                    for script in retained.values()
                    if script.current_version_id
                ]
            )
            validate_import_closure(
                [*scripts, *retained.values()], [*versions, *retained_versions.values()]
            )
            persist_imported_scripts(
                uow, scripts, versions, references, qualifications, replacements, now,
            )
            uow.imports.add_items(items, now)
            strategy_id = activate_strategy(
                uow, MaaStrategyService(self._uow_factory), parsed,
                {**{ordinal: s.script_id for ordinal, s in retained.items()},
                 **{i.archive_ordinal: i.script_id for i in items if i.script_id is not None}},
                batch.strategy_id, correlation_id, now,
            )
            if not uow.imports.complete(
                batch.batch_id, batch.row_version, now, strategy_id=strategy_id,
            ):
                raise MaaDomainError(
                    "archive_import_version_conflict",
                    "Archive import state changed concurrently",
                    409,
                )
            uow.audit.add(
                NewAuditEntry(
                    audit_id=uuid4(),
                    actor_type="system",
                    actor_id=None,
                    action="maa.archive_import.completed",
                    target_type="maa_import_batch",
                    target_id=batch.batch_id,
                    correlation_id=correlation_id,
                    details={
                        "logical_sha256": parsed.logical_sha256,
                        "scripts": len(scripts),
                        "applications": len(applications),
                        "unique_resources": len(ready),
                    },
                    summary="Maa script archive import completed",
                )
            )
            uow.outbox.add(
                NewOutboxEvent(
                    event_id=uuid4(),
                    event_type="maa.archive-import.completed.v1",
                    schema_version=1,
                    aggregate_type="maa_import_batch",
                    aggregate_id=batch.batch_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                    payload={
                        "batch_id": str(batch.batch_id),
                        "logical_sha256": parsed.logical_sha256,
                        "script_count": len(scripts),
                    },
                )
            )
            uow.commit()
            return replace(
                batch,
                status=ImportStatus.COMPLETED,
                completed_at=now,
                row_version=batch.row_version + 1,
                strategy_id=strategy_id,
            )

    def _finalize_blobs(
        self,
        uow: MaaUnitOfWork,
        parsed: ParsedScriptArchive,
        pending: dict[str, NewBlob],
        now: datetime,
    ) -> dict[str, BlobRecord]:
        ready_before = uow.blobs.find_ready_by_sha256_many(tuple(parsed.resources))
        promote = [
            (blob.blob_id, 1) for digest, blob in pending.items() if digest not in ready_before
        ]
        duplicates = [
            (blob.blob_id, 1) for digest, blob in pending.items() if digest in ready_before
        ]
        if uow.blobs.mark_ready_many(promote, now) != len(promote):
            raise MaaDomainError(
                "blob_state_conflict", "A script resource changed state concurrently", 409
            )
        if uow.blobs.mark_quarantined_many(duplicates) != len(duplicates):
            raise MaaDomainError(
                "blob_state_conflict", "A duplicate script resource changed state concurrently", 409
            )
        uow.gc_jobs.schedule_many([(uuid4(), blob_id) for blob_id, _ in duplicates], now)

        ready = uow.blobs.find_ready_by_sha256_many(tuple(parsed.resources))
        if len(ready) != len(parsed.resources):
            raise MaaDomainError(
                "required_resource_missing",
                "Not every archived resource is ready for script publication",
                409,
            )
        return ready


    def _prepare_scripts(self, parsed: ParsedScriptArchive) -> tuple[_PreparedScript, ...]:
        category_names = dict(parsed.categories)
        by_source = {
            script.source_script_id: script for script in parsed.scripts if script.source_script_id
        }
        prepared: list[_PreparedScript] = []
        for archived in parsed.scripts:
            package = archived.application_package
            context = {
                "archive_ordinal": archived.ordinal,
                "script_name": archived.name,
                "application_package": package,
            }
            if package is None or package not in category_names:
                raise MaaDomainError(
                    "archive_category_missing",
                    "Archived script has no declared application category",
                    422,
                    context=context,
                )
            document = copy.deepcopy(archived.document)
            if archived.source_script_id:
                for step in document.get("steps", []):
                    retry = step.get("failure_retry") if isinstance(step, dict) else None
                    if not isinstance(retry, dict) or not retry.get("process_script_id"):
                        continue
                    try:
                        retry["process_script_id"] = str(UUID(str(retry["process_script_id"])))
                    except ValueError as exc:
                        raise MaaDomainError(
                            "archive_recovery_invalid", "Recovery identity is invalid", 422
                        ) from exc
                    dependency = by_source.get(str(retry["process_script_id"]))
                    if (
                        dependency is None
                        or dependency.application_package != package
                        or dependency.script_type != ScriptType.MODULE_PROCESS.value
                    ):
                        raise MaaDomainError(
                            "archive_recovery_missing",
                            "Recovery must be a process script in this archive and application",
                            422,
                        )
            migrations: list[str] = []
            target: dict[str, object] = {"application_package": package}
            original_target = document.get("target")
            if isinstance(original_target, dict) and "screen_size" in original_target:
                target["screen_size"] = original_target["screen_size"]
            if document.get("target") != target:
                migrations.append("legacy_target_rebound")
            document["target"] = target
            validation = validate_script_document(
                document,
                allow_inline_resources=True,
            )
            if not validation.valid or validation.script_type is None:
                issue = validation.issues[0]
                raise MaaDomainError(
                    issue.code,
                    f"Archived script failed static validation at {issue.pointer}",
                    422,
                    context={**context, "pointer": issue.pointer},
                )
            if archived.script_type != validation.script_type.value:
                raise MaaDomainError(
                    "archive_script_type_mismatch", "Archive and document types differ", 422,
                )
            prepared.append(
                _PreparedScript(
                    archived=archived,
                    application_package=package,
                    document=cast(dict[str, object], document),
                    script_type=validation.script_type,
                    migration_codes=tuple(migrations),
                )
            )
        return tuple(prepared)

    def _build_script_facts(
        self,
        prepared_scripts: tuple[_PreparedScript, ...],
        applications: dict[str, ApplicationRecord],
        ready: dict[str, BlobRecord],
        batch_id: UUID,
        correlation_id: UUID,
        now: datetime,
        retained: dict[int, ScriptRecord] | None = None,
        replacements: dict[int, ScriptRecord] | None = None,
        revisions: dict[UUID, int] | None = None,
    ) -> tuple[
        list[ScriptRecord],
        list[ScriptVersionRecord],
        list[ScriptBlobReference],
        list[ScriptQualificationReceipt],
        list[NewImportItem],
    ]:
        scripts: list[ScriptRecord] = []
        versions: list[ScriptVersionRecord] = []
        references: list[ScriptBlobReference] = []
        qualifications: list[ScriptQualificationReceipt] = []
        items: list[NewImportItem] = []
        retained = retained or {}
        replacements = replacements or {}
        revisions = revisions or {}
        identities = {
            prepared.archived.ordinal: retained[prepared.archived.ordinal].script_id
            if prepared.archived.ordinal in retained
            else replacements[prepared.archived.ordinal].script_id
            if prepared.archived.ordinal in replacements else uuid4()
            for prepared in prepared_scripts
        }
        remapping = {
            prepared.archived.source_script_id: str(identities[prepared.archived.ordinal])
            for prepared in prepared_scripts
            if prepared.archived.source_script_id
        }
        for prepared in prepared_scripts:
            archived = prepared.archived
            if archived.ordinal in retained:
                continue
            package = prepared.application_package
            document = copy.deepcopy(prepared.document)
            script_id = identities[archived.ordinal]
            version_id = uuid4()
            if archived.source_script_id:
                document_steps = document.get("steps", [])
                assert isinstance(document_steps, list)  # Already statically validated.
                for step in document_steps:
                    retry = step.get("failure_retry") if isinstance(step, dict) else None
                    if isinstance(retry, dict) and retry.get("process_script_id"):
                        retry["process_script_id"] = remapping[str(retry["process_script_id"])]
            for ordinal, reference in enumerate(archived.resources):
                blob = ready[reference.sha256]
                replace_resource_pointer(
                    document,
                    reference.pointer,
                    {
                        "$blob": str(blob.blob_id),
                        "sha256": reference.sha256,
                        "media_type": reference.media_type,
                        "size_bytes": reference.size_bytes,
                    },
                )
                references.append(
                    ScriptBlobReference(
                        script_version_id=version_id,
                        blob_id=blob.blob_id,
                        json_pointer=reference.pointer,
                        resource_role=reference.role,
                        ordinal=ordinal,
                    )
                )
            script = ScriptRecord(
                script_id=script_id,
                application_id=applications[package].application_id,
                name=archived.name,
                normalized_name=normalize_asset_name(
                    archived.name,
                    asset_kind="script",
                ),
                script_type=prepared.script_type,
                status=ScriptStatus.VALIDATION_PENDING,
                current_version_id=None,
                candidate_version_id=None,
                created_at=now,
                updated_at=now,
                deleted_at=None,
                row_version=1,
            )
            if archived.ordinal in replacements:
                script = replacements[archived.ordinal]
            version = ScriptVersionRecord(
                script_version_id=version_id,
                script_id=script_id,
                revision=revisions.get(script_id, 1),
                schema_version=2,
                manifest_hash=canonical_manifest_hash(document),
                manifest=document,
                created_at=now,
            )
            scripts.append(script)
            versions.append(version)
            qualifications.append(
                ScriptQualificationReceipt(
                    receipt_id=uuid4(),
                    script_version_id=version_id,
                    manifest_hash=version.manifest_hash,
                    kind=QualificationKind.STATIC_CHECK,
                    status=QualificationStatus.PASSED,
                    idempotency_key=f"archive-import:{batch_id}",
                    terminal_id=None,
                    target_device_id=None,
                    executor_version=VALIDATOR_VERSION,
                    error_code=None,
                    diagnostic={"issue_count": 0, "source": "archive_import"},
                    correlation_id=correlation_id,
                    created_at=now,
                )
            )
            items.append(
                NewImportItem(
                    item_id=uuid4(),
                    batch_id=batch_id,
                    archive_ordinal=archived.ordinal,
                    script_name=archived.name,
                    application_package=package,
                    script_id=script_id,
                    script_version_id=version_id,
                    status=ImportItemStatus.IMPORTED,
                    migration_code=",".join(prepared.migration_codes) or None,
                    error_code=None,
                    diagnostic=None,
                )
            )
        return scripts, versions, references, qualifications, items

    def _record_failure(
        self,
        parsed: ParsedScriptArchive,
        pending: dict[str, NewBlob],
        correlation_id: UUID,
        exc: Exception,
        expected_batch_version: int,
    ) -> None:
        now = self._now()
        code = exc.code if isinstance(exc, MaaDomainError) else "archive_import_failed"
        message = exc.message if isinstance(exc, MaaDomainError) else type(exc).__name__
        with self._uow_factory() as uow:
            batch = uow.imports.find_by_logical_hash(parsed.logical_sha256, for_update=True)
            if (
                batch is None
                or batch.status is not ImportStatus.PROCESSING
                or batch.row_version != expected_batch_version
            ):
                return
            expected = [(blob.blob_id, 1) for blob in pending.values()]
            uow.blobs.mark_quarantined_many(expected)
            uow.gc_jobs.schedule_many([(uuid4(), blob.blob_id) for blob in pending.values()], now)
            rejected = _rejected_item(batch.batch_id, exc)
            if rejected is not None:
                uow.imports.add_items([rejected], now)
            if not uow.imports.fail(batch.batch_id, batch.row_version, code, message, now):
                return
            uow.audit.add(
                NewAuditEntry(
                    audit_id=uuid4(),
                    actor_type="system",
                    actor_id=None,
                    action="maa.archive_import.failed",
                    target_type="maa_import_batch",
                    target_id=batch.batch_id,
                    correlation_id=correlation_id,
                    details={"error_code": code, "logical_sha256": parsed.logical_sha256},
                    summary="Maa script archive import failed",
                )
            )
            uow.outbox.add(
                NewOutboxEvent(
                    event_id=uuid4(),
                    event_type="maa.archive-import.failed.v1",
                    schema_version=1,
                    aggregate_type="maa_import_batch",
                    aggregate_id=batch.batch_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                    payload={"batch_id": str(batch.batch_id), "error_code": code},
                )
            )
            uow.commit()


def _rejected_item(batch_id: UUID, exc: Exception) -> NewImportItem | None:
    if not isinstance(exc, MaaDomainError):
        return None
    ordinal = exc.context.get("archive_ordinal")
    script_name = exc.context.get("script_name")
    if not isinstance(ordinal, int) or not isinstance(script_name, str):
        return None
    package = exc.context.get("application_package")
    pointer = exc.context.get("pointer")
    return NewImportItem(
        item_id=uuid4(),
        batch_id=batch_id,
        archive_ordinal=ordinal,
        script_name=script_name,
        application_package=package if isinstance(package, str) else None,
        script_id=None,
        script_version_id=None,
        status=ImportItemStatus.REJECTED,
        migration_code=None,
        error_code=exc.code,
        diagnostic=pointer if isinstance(pointer, str) else exc.message,
    )
