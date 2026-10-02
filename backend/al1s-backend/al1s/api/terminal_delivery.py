from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field

from al1s.api.terminal_auth import authenticated_terminal_id
from al1s.app.service_state import service_state
from al1s.execution.delivery_service import TerminalDeliveryService
from al1s.execution.delivery_types import (
    CancellationAcknowledgementOutcome,
    CommandKind,
    CommandStatus,
    PackageReceiptDisposition,
    PackageStatus,
    TerminalReportDisposition,
    TerminalResultKind,
)

router = APIRouter()


class TerminalCommandResponse(BaseModel):
    command_id: UUID
    kind: CommandKind
    package_id: UUID | None
    attempt_id: UUID
    delivery_no: int
    status: CommandStatus
    payload: dict[str, Any]
    available_at: datetime
    created_at: datetime
    row_version: int


class TerminalCommandPageResponse(BaseModel):
    items: list[TerminalCommandResponse]


class CommandAcknowledgementRequest(BaseModel):
    protocol_version: int = Field(default=1, ge=1)
    report_id: UUID
    outcome: CancellationAcknowledgementOutcome
    occurred_at: datetime


class CommandAcknowledgementResponse(BaseModel):
    report_id: UUID
    disposition: str
    command_status: CommandStatus
    execution_status: str


class TaskPackageResponse(BaseModel):
    package_id: UUID
    attempt_id: UUID
    execution_id: UUID
    package_hash: str
    status: PackageStatus
    body: dict[str, Any]
    row_version: int


class PackageReceiptRequest(BaseModel):
    protocol_version: int = Field(default=1, ge=1)
    report_id: UUID
    command_id: UUID | None = None
    attempt_id: UUID
    disposition: PackageReceiptDisposition
    rejection_code: str | None = Field(default=None, max_length=64)
    diagnostic: str | None = Field(default=None, max_length=512)
    occurred_at: datetime


class PackageReceiptResponse(BaseModel):
    report_id: UUID
    disposition: TerminalReportDisposition
    package_status: PackageStatus
    execution_status: str
    rejection_code: str | None
    offline_start_permit: OfflineStartPermitResponse | None


class OfflineStartPermitResponse(BaseModel):
    permit_id: UUID
    permit_version: int
    token: str
    issued_at: datetime
    expires_at: datetime


class AttemptStartRequest(BaseModel):
    protocol_version: int = Field(default=1, ge=1)
    report_id: UUID
    package_id: UUID
    offline_permit_id: UUID
    offline_permit_token: str = Field(min_length=1, max_length=255)
    occurred_at: datetime


class AttemptStartResponse(BaseModel):
    report_id: UUID
    disposition: TerminalReportDisposition
    execution_status: str
    attempt_status: str
    lease_id: UUID
    lease_version: int
    lease_expires_at: datetime


class AttemptResultRequest(BaseModel):
    diagnostic: dict[str, Any] | None = None
    protocol_version: int = Field(default=1, ge=1)
    report_id: UUID
    package_id: UUID
    lease_id: UUID
    lease_version: int = Field(ge=1)
    result: TerminalResultKind
    error_code: str | None = Field(default=None, max_length=100)
    retryable: bool = False
    occurred_at: datetime


class AttemptResultResponse(BaseModel):
    report_id: UUID
    disposition: TerminalReportDisposition
    execution_status: str
    attempt_status: str
    retry_attempt_id: UUID | None


class PrestartFailureRequest(BaseModel):
    protocol_version: int = Field(default=1, ge=1)
    report_id: UUID
    package_id: UUID
    offline_permit_id: UUID
    occurred_at: datetime


class PrestartFailureResponse(BaseModel):
    report_id: UUID
    disposition: TerminalReportDisposition
    execution_status: str
    attempt_status: str


def _delivery(request: Request) -> TerminalDeliveryService:
    return service_state(request).terminal_delivery


def _correlation_id(request: Request) -> UUID:
    try:
        return UUID(request.state.request_id)
    except ValueError:
        return UUID(int=0)


@router.get("/commands", response_model=TerminalCommandPageResponse)
def list_commands(
    request: Request,
    authorization: str | None = Header(default=None),
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> TerminalCommandPageResponse:
    terminal_id = authenticated_terminal_id(request, authorization)
    commands = _delivery(request).list_commands(terminal_id, limit=limit)
    return TerminalCommandPageResponse(
        items=[
            TerminalCommandResponse(
                command_id=item.command_id,
                kind=item.command_kind,
                package_id=item.package_id,
                attempt_id=item.attempt_id,
                delivery_no=item.delivery_no,
                status=item.status,
                payload=item.payload,
                available_at=item.available_at,
                created_at=item.created_at,
                row_version=item.row_version,
            )
            for item in commands
        ]
    )


@router.post(
    "/commands/{command_id}/acknowledgement",
    response_model=CommandAcknowledgementResponse,
)
def acknowledge_command(
    command_id: UUID,
    body: CommandAcknowledgementRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> CommandAcknowledgementResponse:
    result = _delivery(request).acknowledge_cancel_command(
        terminal_id=authenticated_terminal_id(request, authorization),
        command_id=command_id,
        report_id=body.report_id,
        outcome=body.outcome,
        occurred_at=body.occurred_at,
        correlation_id=_correlation_id(request),
    )
    return CommandAcknowledgementResponse(
        report_id=result.acknowledgement.report_id,
        disposition="accepted",
        command_status=result.command.status,
        execution_status=result.execution.status.value,
    )


@router.get("/task-packages/{package_id}", response_model=TaskPackageResponse)
def get_task_package(
    package_id: UUID,
    request: Request,
    authorization: str | None = Header(default=None),
) -> TaskPackageResponse:
    package = _delivery(request).get_package(
        authenticated_terminal_id(request, authorization), package_id
    )
    return TaskPackageResponse(
        package_id=package.package_id,
        attempt_id=package.attempt_id,
        execution_id=package.execution_id,
        package_hash=package.package_hash,
        status=package.status,
        body=package.manifest,
        row_version=package.row_version,
    )


@router.post(
    "/task-packages/{package_id}/receipt",
    response_model=PackageReceiptResponse,
)
def receive_task_package(
    package_id: UUID,
    body: PackageReceiptRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> PackageReceiptResponse:
    result = _delivery(request).receive_package(
        terminal_id=authenticated_terminal_id(request, authorization),
        package_id=package_id,
        attempt_id=body.attempt_id,
        report_id=body.report_id,
        command_id=body.command_id,
        disposition=body.disposition,
        rejection_code=body.rejection_code,
        diagnostic=body.diagnostic,
        occurred_at=body.occurred_at,
        correlation_id=_correlation_id(request),
    )
    return PackageReceiptResponse(
        report_id=result.report.report_id,
        disposition=result.report.disposition,
        package_status=result.package.status,
        execution_status=result.execution.status.value,
        rejection_code=result.report.error_code,
        offline_start_permit=(
            OfflineStartPermitResponse(
                permit_id=result.offline_start_permit.permit_id,
                permit_version=result.offline_start_permit.permit_version,
                token=result.offline_start_permit.token,
                issued_at=result.offline_start_permit.issued_at,
                expires_at=result.offline_start_permit.expires_at,
            )
            if result.offline_start_permit is not None
            else None
        ),
    )


@router.post("/attempts/{attempt_id}/start", response_model=AttemptStartResponse)
def start_attempt(
    attempt_id: UUID,
    body: AttemptStartRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> AttemptStartResponse:
    result = _delivery(request).start_attempt(
        terminal_id=authenticated_terminal_id(request, authorization),
        attempt_id=attempt_id,
        package_id=body.package_id,
        offline_permit_id=body.offline_permit_id,
        offline_permit_token=body.offline_permit_token,
        report_id=body.report_id,
        occurred_at=body.occurred_at,
        correlation_id=_correlation_id(request),
    )
    return AttemptStartResponse(
        report_id=result.report.report_id,
        disposition=result.report.disposition,
        execution_status=result.execution.status.value,
        attempt_status=result.attempt.status.value,
        lease_id=result.lease.lease_id,
        lease_version=result.lease.row_version,
        lease_expires_at=result.lease.expires_at,
    )


@router.post("/attempts/{attempt_id}/result", response_model=AttemptResultResponse)
def receive_attempt_result(
    attempt_id: UUID,
    body: AttemptResultRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> AttemptResultResponse:
    result = _delivery(request).receive_result(
        terminal_id=authenticated_terminal_id(request, authorization),
        attempt_id=attempt_id,
        package_id=body.package_id,
        report_id=body.report_id,
        lease_id=body.lease_id,
        lease_version=body.lease_version,
        result_kind=body.result,
        diagnostic=body.diagnostic,
        error_code=body.error_code,
        retryable=body.retryable,
        occurred_at=body.occurred_at,
        correlation_id=_correlation_id(request),
    )
    return AttemptResultResponse(
        report_id=result.report.report_id,
        disposition=result.report.disposition,
        execution_status=result.execution.status.value,
        attempt_status=result.attempt.status.value,
        retry_attempt_id=(
            result.retry_attempt.attempt_id if result.retry_attempt is not None else None
        ),
    )


@router.post("/attempts/{attempt_id}/prestart-failure", response_model=PrestartFailureResponse)
def settle_expired_prestart(
    attempt_id: UUID,
    body: PrestartFailureRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> PrestartFailureResponse:
    result = _delivery(request).settle_expired_prestart(
        terminal_id=authenticated_terminal_id(request, authorization),
        attempt_id=attempt_id,
        package_id=body.package_id,
        permit_id=body.offline_permit_id,
        report_id=body.report_id,
        occurred_at=body.occurred_at,
        correlation_id=_correlation_id(request),
    )
    return PrestartFailureResponse(
        report_id=result.report.report_id,
        disposition=result.report.disposition,
        execution_status=result.execution.status.value,
        attempt_status=result.attempt.status.value,
    )
