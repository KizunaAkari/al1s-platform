"""Request and response contracts for the Maa catalog API."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from al1s.api.maa_import_selection import ImportSelectionBody


class ApplicationResponse(BaseModel):
    application_id: UUID
    package_name: str
    display_name: str
    row_version: int
    created_at: datetime
    updated_at: datetime
    icon_data_url: str | None = None


class ApplicationListItemResponse(ApplicationResponse):
    script_count: int


class ApplicationPageResponse(BaseModel):
    items: list[ApplicationListItemResponse]
    next_after_id: UUID | None


class RenameApplicationRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)


class ScriptResponse(BaseModel):
    script_id: UUID
    application_id: UUID
    name: str
    script_type: str
    status: str
    current_version_id: UUID | None
    candidate_version_id: UUID | None
    row_version: int
    created_at: datetime
    updated_at: datetime


class ScriptPageResponse(BaseModel):
    items: list[ScriptResponse]
    next_after_id: UUID | None


class ReorderScriptRequest(BaseModel):
    script_id: UUID
    target_id: UUID
    placement: str = Field(pattern="^(before|after)$")


class CreateEditorScriptRequest(BaseModel):
    device_id: UUID
    name: str | None = Field(default=None, max_length=255)


class EditorScriptCreatedResponse(BaseModel):
    script_id: UUID
    application_id: UUID
    package_name: str
    script_type: str
    created_category: bool


class ApplicationDeviceResponse(BaseModel):
    application_id: UUID
    device_id: UUID
    created_at: datetime


class ApplicationDeviceRequest(BaseModel):
    device_id: UUID


class ApplicableApplicationResponse(BaseModel):
    application_id: UUID
    package_name: str
    display_name: str


class RenameScriptRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class ScriptVersionSummaryResponse(BaseModel):
    script_version_id: UUID
    revision: int
    schema_version: int
    manifest_hash: str
    created_at: datetime


class SaveScriptDocumentRequest(BaseModel):
    manifest: dict[str, Any]
    device_id: UUID


class QualificationSummaryResponse(BaseModel):
    receipt_id: UUID
    kind: str
    status: str
    executor_version: str
    created_at: datetime


class StaticCheckRequest(BaseModel):
    candidate_version_id: UUID


class StaticCheckResponse(QualificationSummaryResponse):
    issues: list[dict[str, str]]


class ScriptVersionPageResponse(BaseModel):
    items: list[ScriptVersionSummaryResponse]
    next_after_revision: int | None


class ScriptVersionDetailResponse(ScriptVersionSummaryResponse):
    script_id: UUID
    manifest: dict[str, Any]


class StrategyResponse(BaseModel):
    strategy_id: UUID
    application_id: UUID
    name: str
    status: str
    current_version_id: UUID | None
    row_version: int
    created_at: datetime
    updated_at: datetime


class StrategyPageResponse(BaseModel):
    items: list[StrategyResponse]
    next_after_id: UUID | None


class StrategyVersionSummaryResponse(BaseModel):
    strategy_version_id: UUID
    revision: int
    schema_version: int
    manifest_hash: str
    created_at: datetime


class StrategyVersionPageResponse(BaseModel):
    items: list[StrategyVersionSummaryResponse]
    next_after_revision: int | None


class StrategyModuleResponse(BaseModel):
    position: int
    module_role: str
    script_version_id: UUID
    wait_after_ms: int


class StrategyVersionDetailResponse(StrategyVersionSummaryResponse):
    strategy_id: UUID
    manifest: dict[str, Any]
    modules: list[StrategyModuleResponse]


class ProcessModuleRequest(BaseModel):
    script_id: UUID
    wait_after_ms: int = Field(default=0, ge=0, le=14_400_000)


class StrategyDefinitionRequest(BaseModel):
    start_script_id: UUID
    process_modules: list[ProcessModuleRequest] = Field(max_length=1_000)
    end_script_id: UUID
    start_wait_after_ms: int = Field(default=0, ge=0, le=14_400_000)
    default_parameters: dict[str, Any] = Field(default_factory=dict)


class CreateStrategyRequest(StrategyDefinitionRequest):
    application_id: UUID
    name: str = Field(min_length=1, max_length=255)


class StrategyEditResponse(BaseModel):
    strategy: StrategyResponse
    definition: StrategyDefinitionRequest


class RenameStrategyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class PublishStrategyRequest(StrategyDefinitionRequest):
    expected_impact_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")


class StrategyMutationResponse(BaseModel):
    strategy: StrategyResponse
    published_version: StrategyVersionSummaryResponse
    modules: list[StrategyModuleResponse]
    reused_version: bool


class ImportBatchResponse(BaseModel):
    batch_id: UUID
    logical_sha256: str
    archive_sha256: str
    archive_schema: str
    status: str
    script_count: int
    application_count: int
    resource_reference_count: int
    unique_resource_count: int
    error_code: str | None
    diagnostic: str | None
    created_at: datetime
    completed_at: datetime | None
    row_version: int


class ImportArchiveRequest(ImportSelectionBody):
    archive_blob_id: UUID


class ImportItemResponse(BaseModel):
    item_id: UUID
    archive_ordinal: int
    script_name: str
    application_package: str | None
    script_id: UUID | None
    script_version_id: UUID | None
    status: str
    migration_code: str | None
    error_code: str | None
    diagnostic: str | None
    created_at: datetime


class ImportItemPageResponse(BaseModel):
    items: list[ImportItemResponse]
    next_after_ordinal: int | None
