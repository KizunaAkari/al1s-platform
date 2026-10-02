from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, status
from pydantic import BaseModel, Field

from al1s.app.service_state import service_state
from al1s.execution.services import ExecutionResourceService
from al1s.execution.types import (
    TargetDeviceMode,
    TargetDeviceRecord,
    TargetIdentifierRecord,
    TargetIdentifierSource,
)

router = APIRouter()


class CreateTargetDeviceRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


class TargetDeviceResponse(BaseModel):
    device_id: UUID
    display_name: str
    platform: str
    mode: TargetDeviceMode
    managing_terminal_id: UUID | None
    row_version: int
    created_at: datetime
    updated_at: datetime
    availability: str = "unknown"
    availability_reason: str | None = None
    availability_observed_at: datetime | None = None


class TargetDevicePageResponse(BaseModel):
    items: list[TargetDeviceResponse]
    next_cursor: UUID | None


class DiscoverIdentifierRequest(BaseModel):
    source_type: TargetIdentifierSource
    identifier: str = Field(min_length=1, max_length=512)
    adb_state: str | None = None


class AdbObservation(BaseModel):
    identifier_id: UUID
    adb_state: Literal["device", "offline", "unauthorized", "other"]


class AdbObservationBatch(BaseModel):
    items: list[AdbObservation] = Field(min_length=1, max_length=100)


class TargetIdentifierResponse(BaseModel):
    identifier_id: UUID
    target_device_id: UUID | None
    source_terminal_id: UUID
    source_type: TargetIdentifierSource
    display_hint: str
    row_version: int
    created_at: datetime
    bound_at: datetime | None
    adb_state: str | None = None
    observed_at: datetime | None = None


class BindIdentifierRequest(BaseModel):
    expected_identifier_version: int = Field(ge=1)


class ConnectDiscoveryRequest(BindIdentifierRequest):
    display_name: str = Field(min_length=1, max_length=120)


class DiscoveryPageResponse(BaseModel):
    items: list[TargetIdentifierResponse]
    next_cursor: UUID | None


class SetTargetModeRequest(BaseModel):
    expected_version: int = Field(ge=1)
    mode: TargetDeviceMode
    managing_terminal_id: UUID | None = None


class RenameTargetDeviceRequest(BaseModel):
    expected_version: int = Field(ge=1)
    display_name: str = Field(min_length=1)


def _service(request: Request) -> ExecutionResourceService:
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


def _target_response(target: TargetDeviceRecord) -> TargetDeviceResponse:
    return TargetDeviceResponse(
        device_id=target.device_id,
        display_name=target.display_name,
        platform=target.platform,
        mode=target.mode,
        managing_terminal_id=target.managing_terminal_id,
        row_version=target.row_version,
        created_at=target.created_at,
        updated_at=target.updated_at,
        availability=target.availability,
        availability_reason=target.availability_reason,
        availability_observed_at=target.availability_observed_at,
    )


def _identifier_response(identifier: TargetIdentifierRecord) -> TargetIdentifierResponse:
    return TargetIdentifierResponse(
        identifier_id=identifier.identifier_id,
        target_device_id=identifier.target_device_id,
        source_terminal_id=identifier.source_terminal_id,
        source_type=identifier.source_type,
        display_hint=identifier.display_hint,
        row_version=identifier.row_version,
        created_at=identifier.created_at,
        bound_at=identifier.bound_at,
        adb_state=identifier.adb_state,
        observed_at=identifier.observed_at,
    )


@router.post("", response_model=TargetDeviceResponse, status_code=status.HTTP_201_CREATED)
def create_target_device(body: CreateTargetDeviceRequest, request: Request) -> TargetDeviceResponse:
    target = _service(request).create_target_device(
        display_name=body.display_name, correlation_id=_correlation_id(request)
    )
    return _target_response(target)


@router.get("", response_model=TargetDevicePageResponse)
def list_target_devices(
    request: Request,
    after_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> TargetDevicePageResponse:
    targets = _service(request).list_target_devices(after_id=after_id, limit=limit)
    return TargetDevicePageResponse(
        items=[_target_response(item) for item in targets],
        next_cursor=targets[-1].device_id if len(targets) == limit else None,
    )


@router.get("/adb-discoveries", response_model=DiscoveryPageResponse)
def list_discoveries(
    request: Request,
    terminal_id: UUID,
    after_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> DiscoveryPageResponse:
    records = _service(request).list_adb_discoveries(
        terminal_id=terminal_id, after_id=after_id, limit=limit
    )
    return DiscoveryPageResponse(
        items=[_identifier_response(item) for item in records],
        next_cursor=records[-1].identifier_id if len(records) == limit else None,
    )


@router.post("/discoveries/{identifier_id}/connect", response_model=TargetDeviceResponse)
def connect_discovery(
    identifier_id: UUID, body: ConnectDiscoveryRequest, request: Request
) -> TargetDeviceResponse:
    target = _service(request).connect_adb_discovery(
        identifier_id=identifier_id,
        expected_identifier_version=body.expected_identifier_version,
        display_name=body.display_name,
        correlation_id=_correlation_id(request),
    )
    return _target_response(target)


@router.post(
    "/discoveries",
    response_model=TargetIdentifierResponse,
    status_code=status.HTTP_201_CREATED,
)
def discover_identifier(
    body: DiscoverIdentifierRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> TargetIdentifierResponse:
    identifier = _service(request).discover_target_identifier(
        credential=_bearer(authorization),
        source_type=body.source_type,
        raw_identifier=body.identifier,
        correlation_id=_correlation_id(request),
        adb_state=body.adb_state,
    )
    return _identifier_response(identifier)


@router.post("/adb-observations")
def report_adb_observations(
    body: AdbObservationBatch,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, int]:
    updated = _service(request).report_adb_observations(
        credential=_bearer(authorization),
        items=tuple((item.identifier_id, item.adb_state) for item in body.items),
    )
    return {"updated": updated}


@router.get("/{device_id}", response_model=TargetDeviceResponse)
def get_target_device(device_id: UUID, request: Request) -> TargetDeviceResponse:
    return _target_response(_service(request).get_target_device(device_id))


@router.patch("/{device_id}/display-name", response_model=TargetDeviceResponse)
def rename_target_device(
    device_id: UUID, body: RenameTargetDeviceRequest, request: Request
) -> TargetDeviceResponse:
    target = _service(request).rename_target_device(
        device_id=device_id, expected_version=body.expected_version,
        display_name=body.display_name, correlation_id=_correlation_id(request),
    )
    return _target_response(target)


@router.post(
    "/{device_id}/identifiers/{identifier_id}/bind",
    response_model=TargetIdentifierResponse,
)
def bind_identifier(
    device_id: UUID,
    identifier_id: UUID,
    body: BindIdentifierRequest,
    request: Request,
) -> TargetIdentifierResponse:
    identifier = _service(request).bind_target_identifier(
        device_id=device_id,
        identifier_id=identifier_id,
        expected_identifier_version=body.expected_identifier_version,
        correlation_id=_correlation_id(request),
    )
    return _identifier_response(identifier)


@router.patch("/{device_id}/mode", response_model=TargetDeviceResponse)
def set_target_mode(
    device_id: UUID, body: SetTargetModeRequest, request: Request
) -> TargetDeviceResponse:
    target = _service(request).set_target_mode(
        device_id=device_id,
        expected_version=body.expected_version,
        mode=body.mode,
        managing_terminal_id=body.managing_terminal_id,
        correlation_id=_correlation_id(request),
    )
    return _target_response(target)
