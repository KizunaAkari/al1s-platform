from __future__ import annotations

from base64 import b64encode
from uuid import UUID

from fastapi import Request

from al1s.api.maa_catalog_schemas import (
    ApplicationResponse,
    ImportBatchResponse,
    ImportItemResponse,
    ProcessModuleRequest,
    ScriptResponse,
    ScriptVersionSummaryResponse,
    StrategyModuleResponse,
    StrategyResponse,
    StrategyVersionSummaryResponse,
)
from al1s.app.service_state import service_state
from al1s.maa.catalog_command_service import MaaCatalogCommandService
from al1s.maa.catalog_service import MaaCatalogQueryService
from al1s.maa.errors import MaaDomainError
from al1s.maa.import_command_service import MaaArchiveImportCommandService
from al1s.maa.publication_service import MaaScriptPublicationService
from al1s.maa.script_command_service import MaaScriptCommandService
from al1s.maa.strategy_command_service import MaaStrategyCommandService
from al1s.maa.strategy_service import MaaStrategyService
from al1s.maa.types import (
    ApplicationRecord,
    ImportBatchRecord,
    ImportItemRecord,
    ProcessModuleInput,
    ScriptRecord,
    ScriptVersionRecord,
    StrategyModuleRecord,
    StrategyRecord,
    StrategyVersionRecord,
)


def _catalog(request: Request) -> MaaCatalogQueryService:
    return service_state(request).maa_catalog


def _commands(request: Request) -> MaaCatalogCommandService:
    return service_state(request).maa_catalog_commands


def _script_commands(request: Request) -> MaaScriptCommandService:
    return service_state(request).maa_script_commands


def _publication(request: Request) -> MaaScriptPublicationService:
    return service_state(request).maa_publication


def _strategies(request: Request) -> MaaStrategyService:
    return service_state(request).maa_strategies


def _strategy_commands(request: Request) -> MaaStrategyCommandService:
    return service_state(request).maa_strategy_commands


def _import_commands(request: Request) -> MaaArchiveImportCommandService:
    return service_state(request).maa_archive_import_commands


def _correlation_id(request: Request) -> UUID:
    try:
        return UUID(request.state.request_id)
    except ValueError:
        return UUID(int=0)


def _if_match_row_version(value: str) -> int:
    token = value.strip()
    if token.startswith("W/"):
        raise MaaDomainError(
            "invalid_if_match", "Weak ETags are not accepted for Maa mutations", 422
        )
    if len(token) >= 2 and token[0] == token[-1] == '"':
        token = token[1:-1]
    if not token.isdecimal() or int(token) < 1:
        raise MaaDomainError(
            "invalid_if_match",
            "If-Match must contain a positive row version",
            422,
        )
    return int(token)


def _application(item: ApplicationRecord) -> ApplicationResponse:
    return ApplicationResponse(
        application_id=item.application_id,
        package_name=item.package_name,
        display_name=item.display_name,
        row_version=item.row_version,
        created_at=item.created_at,
        updated_at=item.updated_at,
        icon_data_url=(
            "data:image/png;base64," + b64encode(item.icon_png).decode("ascii")
            if item.icon_png is not None
            else None
        ),
    )


def _script(item: ScriptRecord) -> ScriptResponse:
    return ScriptResponse(
        script_id=item.script_id,
        application_id=item.application_id,
        name=item.name,
        script_type=item.script_type.value,
        status=item.status.value,
        current_version_id=item.current_version_id,
        candidate_version_id=item.candidate_version_id,
        row_version=item.row_version,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _script_version(item: ScriptVersionRecord) -> ScriptVersionSummaryResponse:
    return ScriptVersionSummaryResponse(
        script_version_id=item.script_version_id,
        revision=item.revision,
        schema_version=item.schema_version,
        manifest_hash=item.manifest_hash,
        created_at=item.created_at,
    )


def _process_modules(
    items: list[ProcessModuleRequest],
) -> list[ProcessModuleInput]:
    return [
        ProcessModuleInput(
            script_id=item.script_id,
            wait_after_ms=item.wait_after_ms,
        )
        for item in items
    ]


def _strategy(item: StrategyRecord) -> StrategyResponse:
    return StrategyResponse(
        strategy_id=item.strategy_id,
        application_id=item.application_id,
        name=item.name,
        status=item.status.value,
        current_version_id=item.current_version_id,
        row_version=item.row_version,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _strategy_version(item: StrategyVersionRecord) -> StrategyVersionSummaryResponse:
    return StrategyVersionSummaryResponse(
        strategy_version_id=item.strategy_version_id,
        revision=item.revision,
        schema_version=item.schema_version,
        manifest_hash=item.manifest_hash,
        created_at=item.created_at,
    )


def _strategy_module(item: StrategyModuleRecord) -> StrategyModuleResponse:
    return StrategyModuleResponse(
        position=item.position,
        module_role=item.module_role.value,
        script_version_id=item.script_version_id,
        wait_after_ms=item.wait_after_ms,
    )


def _import_batch(item: ImportBatchRecord) -> ImportBatchResponse:
    return ImportBatchResponse(
        batch_id=item.batch_id,
        logical_sha256=item.logical_sha256,
        archive_sha256=item.archive_sha256,
        archive_schema=item.archive_schema,
        status=item.status.value,
        script_count=item.script_count,
        application_count=item.application_count,
        resource_reference_count=item.resource_reference_count,
        unique_resource_count=item.unique_resource_count,
        error_code=item.error_code,
        diagnostic=item.diagnostic,
        created_at=item.created_at,
        completed_at=item.completed_at,
        row_version=item.row_version,
    )


def _import_item(item: ImportItemRecord) -> ImportItemResponse:
    return ImportItemResponse(
        item_id=item.item_id,
        archive_ordinal=item.archive_ordinal,
        script_name=item.script_name,
        application_package=item.application_package,
        script_id=item.script_id,
        script_version_id=item.script_version_id,
        status=item.status.value,
        migration_code=item.migration_code,
        error_code=item.error_code,
        diagnostic=item.diagnostic,
        created_at=item.created_at,
    )
