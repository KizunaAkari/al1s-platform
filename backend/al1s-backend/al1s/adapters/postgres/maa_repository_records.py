"""Typed row-to-domain projections shared by Maa repositories."""

from __future__ import annotations

from al1s.adapters.postgres.maa_models import (
    MaaApplicationRow,
    MaaImportBatchRow,
    MaaImportItemRow,
    MaaMutationReceiptRow,
    MaaQuickTestSessionRow,
    MaaScriptRow,
    MaaScriptVersionRow,
    MaaStrategyRow,
    MaaStrategyVersionRow,
)
from al1s.maa.types import (
    ApplicationRecord,
    ImportBatchRecord,
    ImportItemRecord,
    ImportItemStatus,
    ImportStatus,
    MutationReceiptRecord,
    QuickTestSessionRecord,
    QuickTestSessionStatus,
    ScriptRecord,
    ScriptStatus,
    ScriptType,
    ScriptVersionRecord,
    StrategyRecord,
    StrategyStatus,
    StrategyVersionRecord,
)


def _application_record(row: MaaApplicationRow) -> ApplicationRecord:
    return ApplicationRecord(
        application_id=row.id,
        package_name=row.package_name,
        display_name=row.display_name,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
        row_version=row.row_version,
        icon_png=row.icon_png,
    )

def _script_record(row: MaaScriptRow) -> ScriptRecord:
    return ScriptRecord(
        script_id=row.id,
        application_id=row.application_id,
        name=row.name,
        normalized_name=row.normalized_name,
        script_type=ScriptType(row.script_type),
        status=ScriptStatus(row.status),
        current_version_id=row.current_version_id,
        candidate_version_id=row.candidate_version_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
        row_version=row.row_version,
    )

def _import_record(row: MaaImportBatchRow) -> ImportBatchRecord:
    return ImportBatchRecord(
        batch_id=row.id,
        logical_sha256=row.logical_sha256,
        archive_sha256=row.archive_sha256,
        archive_schema=row.archive_schema,
        status=ImportStatus(row.status),
        script_count=row.script_count,
        application_count=row.application_count,
        resource_reference_count=row.resource_reference_count,
        unique_resource_count=row.unique_resource_count,
        error_code=row.error_code,
        diagnostic=row.diagnostic,
        created_at=row.created_at,
        completed_at=row.completed_at,
        row_version=row.row_version,
        strategy_id=row.strategy_id,
    )

def _import_item_record(row: MaaImportItemRow) -> ImportItemRecord:
    return ImportItemRecord(
        item_id=row.id,
        batch_id=row.batch_id,
        archive_ordinal=row.archive_ordinal,
        script_name=row.script_name,
        application_package=row.application_package,
        script_id=row.script_id,
        script_version_id=row.script_version_id,
        status=ImportItemStatus(row.status),
        migration_code=row.migration_code,
        error_code=row.error_code,
        diagnostic=row.diagnostic,
        created_at=row.created_at,
    )

def _mutation_receipt_record(row: MaaMutationReceiptRow) -> MutationReceiptRecord:
    return MutationReceiptRecord(
        receipt_id=row.id,
        operation=row.operation,
        idempotency_key=row.idempotency_key,
        request_hash=row.request_hash,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        response_status=row.response_status,
        response_body=row.response_body,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )

def _version_record(row: MaaScriptVersionRow) -> ScriptVersionRecord:
    return ScriptVersionRecord(
        script_version_id=row.id,
        script_id=row.script_id,
        revision=row.revision,
        schema_version=row.schema_version,
        manifest_hash=row.manifest_hash,
        manifest=row.manifest,
        created_at=row.created_at,
    )

def _strategy_record(row: MaaStrategyRow) -> StrategyRecord:
    return StrategyRecord(
        strategy_id=row.id,
        application_id=row.application_id,
        name=row.name,
        normalized_name=row.normalized_name,
        status=StrategyStatus(row.status),
        current_version_id=row.current_version_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
        row_version=row.row_version,
    )

def _strategy_version_record(row: MaaStrategyVersionRow) -> StrategyVersionRecord:
    return StrategyVersionRecord(
        strategy_version_id=row.id,
        strategy_id=row.strategy_id,
        revision=row.revision,
        schema_version=row.schema_version,
        manifest_hash=row.manifest_hash,
        manifest=row.manifest,
        created_at=row.created_at,
    )

def _quick_test_record(row: MaaQuickTestSessionRow) -> QuickTestSessionRecord:
    return QuickTestSessionRecord(
        session_id=row.id,
        script_id=row.script_id,
        script_version_id=row.script_version_id,
        manifest_hash=row.manifest_hash,
        definition_hash=row.definition_hash,
        definition=row.definition,
        terminal_id=row.terminal_id,
        target_device_id=row.target_device_id,
        status=QuickTestSessionStatus(row.status),
        request_idempotency_key=row.request_idempotency_key,
        expires_at=row.expires_at,
        claimed_at=row.claimed_at,
        started_at=row.started_at,
        cancel_requested_at=row.cancel_requested_at,
        completed_at=row.completed_at,
        qualification_receipt_id=row.qualification_receipt_id,
        created_at=row.created_at,
        row_version=row.row_version,
    )
