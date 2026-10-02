from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field

from al1s.api.terminal_auth import authenticated_terminal_id
from al1s.app.service_state import service_state
from al1s.execution.scheduling_types import ResolvedExecutionDefinition
from al1s.maa.quick_test_service import MaaQuickTestService
from al1s.maa.types import QuickTestEventRecord, QuickTestSessionRecord, QuickTestSessionStatus

router = APIRouter()


class TerminalQuickTestSummary(BaseModel):
    session_id: UUID
    script_id: UUID
    candidate_version_id: UUID
    candidate_manifest_hash: str
    definition_hash: str
    target_device_id: UUID
    status: QuickTestSessionStatus
    expires_at: datetime
    row_version: int


class TerminalQuickTestPage(BaseModel):
    items: list[TerminalQuickTestSummary]


class TerminalQuickTestDefinition(BaseModel):
    revision_id: str
    schema_version: int
    manifest_hash: str
    manifest: dict[str, Any]
    capability_requirements: dict[str, Any]
    blobs: list[dict[str, Any]]


class TerminalQuickTestClaim(BaseModel):
    session: TerminalQuickTestSummary
    definition: TerminalQuickTestDefinition


class TerminalQuickTestControl(BaseModel):
    status: QuickTestSessionStatus
    cancel_requested: bool


class TerminalQuickTestEvent(BaseModel):
    sequence: int = Field(ge=1, le=1000)
    kind: str
    step_number: int | None = Field(default=None, ge=1, le=1000)
    code: str | None = Field(default=None, max_length=100)
    created_at: datetime


class TerminalQuickTestEventBatch(BaseModel):
    items: list[TerminalQuickTestEvent] = Field(min_length=1, max_length=50)


class TerminalQuickTestEventReceipt(BaseModel):
    last_sequence: int


def _service(request: Request) -> MaaQuickTestService:
    return service_state(request).maa_quick_tests


@router.get("/quick-tests", response_model=TerminalQuickTestPage)
def list_quick_tests(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    authorization: str | None = Header(default=None),
) -> TerminalQuickTestPage:
    terminal_id = authenticated_terminal_id(request, authorization)
    pending = _service(request).list_pending(terminal_id, limit=limit)
    return TerminalQuickTestPage(items=[_summary(item.session) for item in pending])


@router.post("/quick-tests/{session_id}/claim", response_model=TerminalQuickTestClaim)
def claim_quick_test(
    session_id: UUID,
    request: Request,
    authorization: str | None = Header(default=None),
) -> TerminalQuickTestClaim:
    terminal_id = authenticated_terminal_id(request, authorization)
    claimed = _service(request).claim(session_id, terminal_id)
    return TerminalQuickTestClaim(
        session=_summary(claimed.session),
        definition=_definition(claimed.definition),
    )


@router.get("/quick-tests/{session_id}/control", response_model=TerminalQuickTestControl)
def get_quick_test_control(
    session_id: UUID, request: Request, authorization: str | None = Header(default=None),
) -> TerminalQuickTestControl:
    terminal_id = authenticated_terminal_id(request, authorization)
    session = _service(request).get_control(session_id=session_id, terminal_id=terminal_id)
    return TerminalQuickTestControl(
        status=session.status, cancel_requested=session.cancel_requested_at is not None
    )


@router.post("/quick-tests/{session_id}/events", response_model=TerminalQuickTestEventReceipt)
def report_quick_test_events(
    session_id: UUID, body: TerminalQuickTestEventBatch, request: Request,
    authorization: str | None = Header(default=None),
) -> TerminalQuickTestEventReceipt:
    terminal_id = authenticated_terminal_id(request, authorization)
    last = _service(request).report_events(
        session_id=session_id, terminal_id=terminal_id,
        events=tuple(QuickTestEventRecord(
            session_id=session_id, sequence=item.sequence, kind=item.kind,
            step_number=item.step_number, code=item.code,
            created_at=item.created_at,
        ) for item in body.items),
    )
    return TerminalQuickTestEventReceipt(last_sequence=last)


def _summary(session: QuickTestSessionRecord) -> TerminalQuickTestSummary:
    return TerminalQuickTestSummary(
        session_id=session.session_id,
        script_id=session.script_id,
        candidate_version_id=session.script_version_id,
        candidate_manifest_hash=session.manifest_hash,
        definition_hash=session.definition_hash,
        target_device_id=session.target_device_id,
        status=session.status,
        expires_at=session.expires_at,
        row_version=session.row_version,
    )


def _definition(definition: ResolvedExecutionDefinition) -> TerminalQuickTestDefinition:
    requirements = definition.capability_requirements
    return TerminalQuickTestDefinition(
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
            {
                "blob_id": item.blob_id,
                "resource_key": item.resource_key,
                "role": item.role,
            }
            for item in definition.blobs
        ],
    )
