from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from al1s.app.service_state import service_state
from al1s.execution.recording_download import RecordingPage
from al1s.execution.scheduling_types import ResolvedExecutionDefinition
from al1s.execution.services import ExecutionResourceService
from al1s.maa.quick_test_diagnostics import QuickTestFailureDetail, project_failure_detail
from al1s.maa.quick_test_service import MaaQuickTestService
from al1s.maa.types import QualificationStatus, QuickTestSessionStatus

router = APIRouter()


class QuickTestDefinitionRequest(BaseModel):
    candidate_version_id: UUID
    terminal_id: UUID
    target_device_id: UUID
    ttl_seconds: int = Field(default=900, ge=60, le=14_400)
    step_number: int | None = Field(default=None, ge=1, le=1000)


class DefinitionBlobResponse(BaseModel):
    blob_id: UUID
    resource_key: str
    role: str


class ExecutionDefinitionResponse(BaseModel):
    revision_id: str
    schema_version: int
    manifest_hash: str
    manifest: dict[str, Any]
    capability_requirements: dict[str, Any]
    blobs: list[DefinitionBlobResponse]


class QuickTestDefinitionResponse(BaseModel):
    session_id: UUID
    status: QuickTestSessionStatus
    script_id: UUID
    candidate_version_id: UUID
    candidate_manifest_hash: str
    terminal_id: UUID
    target_device_id: UUID
    expires_at: datetime
    definition: ExecutionDefinitionResponse


class QuickTestResultRequest(BaseModel):
    session_id: UUID
    candidate_version_id: UUID
    candidate_manifest_hash: str = Field(pattern="^[0-9a-f]{64}$")
    definition_hash: str = Field(pattern="^[0-9a-f]{64}$")
    executor_version: str = Field(min_length=1, max_length=100)
    passed: bool
    error_code: str | None = Field(default=None, max_length=100)
    diagnostic: dict[str, Any] = Field(default_factory=dict)


class QuickTestResultResponse(BaseModel):
    session_id: UUID
    session_status: QuickTestSessionStatus
    receipt_id: UUID
    qualification_status: QualificationStatus
    completed_at: datetime


class QuickTestDetailResponse(BaseModel):
    session_id: UUID
    candidate_version_id: UUID
    status: QuickTestSessionStatus
    terminal_id: UUID
    target_device_id: UUID
    expires_at: datetime
    completed_at: datetime | None
    started_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    qualification_status: QualificationStatus | None
    error_code: str | None
    failed_step_number: int | None
    step_number: int | None = None
    failure_detail: QuickTestFailureDetail | None = None


@router.get("/scripts/{script_id}/quick-tests/{session_id}", response_model=QuickTestDetailResponse)
def get_quick_test_detail(
    script_id: UUID, session_id: UUID, request: Request,
) -> QuickTestDetailResponse:
    detail = _quick_tests(request).get_detail(script_id=script_id, session_id=session_id)
    session, receipt = detail.session, detail.receipt
    # Return a bounded diagnostic projection, not arbitrary executor data or resources.
    failed_step = receipt.diagnostic.get("failed_step") if receipt else None
    number = failed_step.get("number") if isinstance(failed_step, dict) else None
    selected = session.definition["manifest"].get("debug_step_number")
    if selected is not None and number is not None:
        number = selected
    return QuickTestDetailResponse(
        session_id=session.session_id, candidate_version_id=session.script_version_id,
        status=session.status, terminal_id=session.terminal_id,
        target_device_id=session.target_device_id, expires_at=session.expires_at,
        completed_at=session.completed_at,
        started_at=session.started_at, cancel_requested_at=session.cancel_requested_at,
        qualification_status=receipt.status if receipt else None,
        error_code=receipt.error_code[:100] if receipt and receipt.error_code else None,
        failed_step_number=number if type(number) is int and 1 <= number <= 1000 else None,
        step_number=selected,
        failure_detail=project_failure_detail(session.definition, receipt.diagnostic)
        if receipt and receipt.status == QualificationStatus.FAILED else None,
    )


class QuickTestEventResponse(BaseModel):
    sequence: int
    kind: str
    step_number: int | None
    code: str | None
    rule_name: str | None = None
    created_at: datetime


class QuickTestEventPage(BaseModel):
    items: list[QuickTestEventResponse]
    next_after: int | None


@router.post(
    "/scripts/{script_id}/quick-tests/{session_id}/stop",
    response_model=QuickTestDetailResponse,
)
def stop_quick_test(script_id: UUID, session_id: UUID, request: Request) -> QuickTestDetailResponse:
    _quick_tests(request).request_cancel(script_id=script_id, session_id=session_id)
    return get_quick_test_detail(script_id, session_id, request)


@router.get(
    "/scripts/{script_id}/quick-tests/{session_id}/events",
    response_model=QuickTestEventPage,
)
def list_quick_test_events(
    script_id: UUID, session_id: UUID, request: Request,
    after: Annotated[int, Query(ge=0, le=1000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> QuickTestEventPage:
    events = _quick_tests(request).list_events(
        script_id=script_id, session_id=session_id, after=after, limit=limit
    )
    return QuickTestEventPage(
        items=[QuickTestEventResponse(
            sequence=item.sequence, kind=item.kind, step_number=item.step_number,
            code=item.code, created_at=item.created_at, rule_name=getattr(item, "rule_name", None),
        ) for item in events],
        next_after=events[-1].sequence if len(events) == limit else None,
    )


@router.get("/scripts/{script_id}/quick-tests/{session_id}/screenshots")
def quick_test_screenshots(
    script_id: UUID, session_id: UUID, request: Request,
    cursor: UUID | None = None, limit: int = 20,
) -> RecordingPage:
    _quick_tests(request).get_detail(script_id=script_id, session_id=session_id)
    media = service_state(request).quick_test_media
    return media.list_recordings(session_id, cursor, limit)


@router.get("/scripts/{script_id}/quick-tests/{session_id}/screenshots/{artifact_id}/download")
def download_quick_test_screenshot(
    script_id: UUID, session_id: UUID, artifact_id: UUID, request: Request,
) -> StreamingResponse:
    _quick_tests(request).get_detail(script_id=script_id, session_id=session_id)
    media = service_state(request).quick_test_media
    item = media.metadata(session_id, artifact_id)
    return StreamingResponse(
        media.stream(session_id, artifact_id, item),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(item.file_name, safe='')}",
            "Content-Length": str(item.size_bytes),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Content-SHA256": item.sha256,
        },
    )


def _quick_tests(request: Request) -> MaaQuickTestService:
    return service_state(request).maa_quick_tests


def _resources(request: Request) -> ExecutionResourceService:
    return service_state(request).execution_resources


def _correlation_id(request: Request) -> UUID:
    try:
        return UUID(request.state.request_id)
    except ValueError:
        return UUID(int=0)


def _bearer(authorization: str | None) -> str:
    if authorization is None:
        return ""
    scheme, separator, value = authorization.partition(" ")
    return value if separator and scheme.lower() == "bearer" else ""


def _definition_response(
    definition: ResolvedExecutionDefinition,
) -> ExecutionDefinitionResponse:
    requirements = definition.capability_requirements
    return ExecutionDefinitionResponse(
        revision_id=definition.revision_id,
        schema_version=definition.schema_version,
        manifest_hash=definition.manifest_hash,
        manifest=definition.manifest,
        capability_requirements={
            "schema_version": requirements.schema_version,
            "min_protocol_version": requirements.min_protocol_version,
            "architectures": list(requirements.architectures),
            "min_memory_bytes": requirements.min_memory_bytes,
            "min_storage_bytes": requirements.min_storage_bytes,
            "accelerator_type": requirements.accelerator_type,
            "provider_keys": list(requirements.provider_keys),
            "requires_target_device": requirements.requires_target_device,
        },
        blobs=[
            DefinitionBlobResponse(
                blob_id=item.blob_id,
                resource_key=item.resource_key,
                role=item.role,
            )
            for item in definition.blobs
        ],
    )


@router.post(
    "/scripts/{script_id}/quick-test-definition",
    response_model=QuickTestDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
)
def issue_quick_test_definition(
    script_id: UUID,
    body: QuickTestDefinitionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> QuickTestDefinitionResponse:
    _resources(request).require_quick_test_assignment(
        terminal_id=body.terminal_id,
        target_device_id=body.target_device_id,
    )
    issued = _quick_tests(request).issue(
        script_id=script_id,
        candidate_version_id=body.candidate_version_id,
        terminal_id=body.terminal_id,
        target_device_id=body.target_device_id,
        idempotency_key=idempotency_key,
        ttl=timedelta(seconds=body.ttl_seconds),
        step_number=body.step_number,
    )
    session = issued.session
    return QuickTestDefinitionResponse(
        session_id=session.session_id,
        status=session.status,
        script_id=session.script_id,
        candidate_version_id=session.script_version_id,
        candidate_manifest_hash=session.manifest_hash,
        terminal_id=session.terminal_id,
        target_device_id=session.target_device_id,
        expires_at=session.expires_at,
        definition=_definition_response(issued.definition),
    )


@router.post(
    "/scripts/{script_id}/quick-test-results",
    response_model=QuickTestResultResponse,
)
def record_quick_test_result(
    script_id: UUID,
    body: QuickTestResultRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    authorization: Annotated[str | None, Header()] = None,
) -> QuickTestResultResponse:
    terminal = _resources(request).authenticate_terminal(_bearer(authorization))
    completed = _quick_tests(request).record_result(
        session_id=body.session_id,
        script_id=script_id,
        authenticated_terminal_id=terminal.terminal_id,
        candidate_version_id=body.candidate_version_id,
        manifest_hash=body.candidate_manifest_hash,
        definition_hash=body.definition_hash,
        executor_version=body.executor_version,
        passed=body.passed,
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
        error_code=body.error_code,
        diagnostic=body.diagnostic,
    )
    assert completed.session.completed_at is not None
    return QuickTestResultResponse(
        session_id=completed.session.session_id,
        session_status=completed.session.status,
        receipt_id=completed.receipt.receipt_id,
        qualification_status=completed.receipt.status,
        completed_at=completed.session.completed_at,
    )
