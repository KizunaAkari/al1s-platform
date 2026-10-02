from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response, status
from pydantic import BaseModel, Field

from al1s.app.service_state import service_state
from al1s.execution.errors import AuthenticationError
from al1s.execution.security import parse_terminal_credential
from al1s.execution.services import ExecutionResourceService
from al1s.execution.storage import StorageObservation
from al1s.execution.types import (
    ActionAvailability,
    CapabilityManifest,
    TargetDeviceRecord,
    TerminalAcceptanceStatus,
    TerminalManagementRecord,
    TerminalRecord,
    TerminalServiceStatus,
    TerminalType,
)

router = APIRouter()


class CreateRegistrationGrantRequest(BaseModel):
    allowed_terminal_type: TerminalType | None = None
    target_device_id: UUID | None = None
    ttl_seconds: int = Field(default=900, ge=60, le=86400)


class RegistrationGrantResponse(BaseModel):
    grant_id: UUID
    registration_code: str
    expires_at: datetime
    target_device_id: UUID | None


class RegisterTerminalRequest(BaseModel):
    registration_code: str = Field(min_length=20, max_length=512)
    installation_id: UUID
    terminal_type: TerminalType
    display_name: str = Field(min_length=1, max_length=120)
    agent_version: str = Field(min_length=1, max_length=64)


class TerminalResponse(BaseModel):
    terminal_id: UUID
    installation_id: UUID
    terminal_type: TerminalType
    display_name: str
    service_status: TerminalServiceStatus
    acceptance_status: TerminalAcceptanceStatus
    agent_version: str
    current_capability_profile_id: UUID | None
    row_version: int
    name_version: int
    created_at: datetime
    last_seen_at: datetime | None
    storage: StorageObservation | None = None
    storage_observed_at: datetime | None = None
    storage_probe_ok: bool = False


class ActionAvailabilityResponse(BaseModel):
    allowed: bool
    refusal_code: str | None
    refusal_message: str | None


class TerminalManagementResponse(TerminalResponse):
    delete: ActionAvailabilityResponse


class RegisteredTargetDeviceResponse(BaseModel):
    device_id: UUID
    display_name: str
    mode: str
    managing_terminal_id: UUID | None


class RegisteredTerminalResponse(BaseModel):
    terminal: TerminalResponse
    credential: str
    target_device: RegisteredTargetDeviceResponse | None


class TerminalPageResponse(BaseModel):
    items: list[TerminalManagementResponse]
    next_cursor: UUID | None


class HeartbeatRequest(BaseModel):
    expected_version: int = Field(ge=1)
    service_status: TerminalServiceStatus
    acceptance_status: TerminalAcceptanceStatus
    agent_version: str = Field(min_length=1, max_length=64)
    storage: StorageObservation | None = None


class RenameTerminalRequest(BaseModel):
    expected_name_version: int = Field(ge=0)
    display_name: str = Field(min_length=1, max_length=512)


class CapabilityProfileRequest(BaseModel):
    revision: int = Field(ge=1)
    schema_version: int = Field(ge=1)
    protocol_version: int = Field(ge=1)
    agent_version: str = Field(min_length=1, max_length=64)
    os_name: str = Field(min_length=1, max_length=64)
    os_version: str = Field(min_length=1, max_length=128)
    architecture: str = Field(min_length=1, max_length=64)
    cpu_cores: int = Field(ge=1, le=4096)
    memory_bytes: int = Field(ge=0)
    storage_available_bytes: int = Field(ge=0)
    accelerator_type: str | None = Field(default=None, max_length=64)
    low_resource: bool = False
    provider_keys: list[str] = Field(default_factory=list, max_length=128)
    details: dict[str, Any] = Field(default_factory=dict)


class CapabilityProfileResponse(BaseModel):
    profile_id: UUID
    terminal_id: UUID
    revision: int
    manifest_hash: str
    created_at: datetime


class RotateCredentialRequest(BaseModel):
    expected_version: int = Field(ge=1)


class CapabilityDetailResponse(BaseModel):
    profile_id: UUID
    revision: int
    observed_at: datetime
    os_name: str
    architecture: str
    cpu_cores: int
    memory_bytes: int
    storage_available_bytes: int
    provider_keys: list[str]
    adb_online: int
    adb_offline: int
    adb_unauthorized: int


@router.get("/{terminal_id}/capability-profile", response_model=CapabilityDetailResponse | None)
def capability_detail(terminal_id: UUID, request: Request) -> CapabilityDetailResponse | None:
    profile = _service(request).current_capability(terminal_id)
    if profile is None:
        return None
    manifest = profile.manifest
    adb = manifest.details.get("adb")
    counts = adb if isinstance(adb, dict) else {}

    def count(key: str) -> int:
        value = counts.get(key)
        return value if type(value) is int and 0 <= value <= 10000 else 0

    return CapabilityDetailResponse(
        profile_id=profile.profile_id, revision=profile.revision, observed_at=profile.created_at,
        os_name=manifest.os_name, architecture=manifest.architecture, cpu_cores=manifest.cpu_cores,
        memory_bytes=manifest.memory_bytes,
        storage_available_bytes=manifest.storage_available_bytes,
        provider_keys=list(manifest.provider_keys), adb_online=count("online_devices"),
        adb_offline=count("offline_devices"), adb_unauthorized=count("unauthorized_devices"),
    )


class RotatedCredentialResponse(BaseModel):
    terminal: TerminalResponse
    credential: str


class DeleteTerminalRequest(BaseModel):
    expected_version: int = Field(ge=1)


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


def _terminal_credential(authorization: str | None, terminal_id: UUID) -> str:
    credential = _bearer(authorization)
    if parse_terminal_credential(credential).identifier != terminal_id:
        raise AuthenticationError()
    return credential


def _terminal_response(terminal: TerminalRecord) -> TerminalResponse:
    return TerminalResponse(
        terminal_id=terminal.terminal_id,
        installation_id=terminal.installation_id,
        terminal_type=terminal.terminal_type,
        display_name=terminal.display_name,
        service_status=terminal.service_status,
        acceptance_status=terminal.acceptance_status,
        agent_version=terminal.agent_version,
        current_capability_profile_id=terminal.current_capability_profile_id,
        row_version=terminal.row_version,
        name_version=terminal.name_version,
        created_at=terminal.created_at,
        last_seen_at=terminal.last_seen_at,
        storage=terminal.storage, storage_observed_at=terminal.storage_observed_at,
        storage_probe_ok=terminal.storage_probe_ok,
    )


def _registered_target_response(
    target: TargetDeviceRecord | None,
) -> RegisteredTargetDeviceResponse | None:
    if target is None:
        return None
    return RegisteredTargetDeviceResponse(
        device_id=target.device_id,
        display_name=target.display_name,
        mode=target.mode.value,
        managing_terminal_id=target.managing_terminal_id,
    )


def _action_response(action: ActionAvailability) -> ActionAvailabilityResponse:
    return ActionAvailabilityResponse(
        allowed=action.allowed,
        refusal_code=action.refusal_code,
        refusal_message=action.refusal_message,
    )


def _terminal_management_response(item: TerminalManagementRecord) -> TerminalManagementResponse:
    terminal = item.terminal
    return TerminalManagementResponse(
        terminal_id=terminal.terminal_id,
        installation_id=terminal.installation_id,
        terminal_type=terminal.terminal_type,
        display_name=terminal.display_name,
        service_status=terminal.service_status,
        acceptance_status=terminal.acceptance_status,
        agent_version=terminal.agent_version,
        current_capability_profile_id=terminal.current_capability_profile_id,
        row_version=terminal.row_version,
        name_version=terminal.name_version,
        created_at=terminal.created_at,
        last_seen_at=terminal.last_seen_at,
        storage=terminal.storage, storage_observed_at=terminal.storage_observed_at,
        storage_probe_ok=terminal.storage_probe_ok,
        delete=_action_response(item.delete),
    )


@router.post(
    "/registration-grants",
    response_model=RegistrationGrantResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_registration_grant(
    body: CreateRegistrationGrantRequest, request: Request
) -> RegistrationGrantResponse:
    grant = _service(request).create_registration_grant(
        allowed_terminal_type=body.allowed_terminal_type,
        target_device_id=body.target_device_id,
        ttl=timedelta(seconds=body.ttl_seconds),
        correlation_id=_correlation_id(request),
    )
    return RegistrationGrantResponse(
        grant_id=grant.grant_id,
        registration_code=grant.registration_code,
        expires_at=grant.expires_at,
        target_device_id=grant.target_device_id,
    )


@router.post(
    "/register",
    response_model=RegisteredTerminalResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_terminal(
    body: RegisterTerminalRequest, request: Request
) -> RegisteredTerminalResponse:
    result = _service(request).register_terminal(
        registration_code=body.registration_code,
        installation_id=body.installation_id,
        terminal_type=body.terminal_type,
        display_name=body.display_name,
        agent_version=body.agent_version,
        correlation_id=_correlation_id(request),
    )
    return RegisteredTerminalResponse(
        terminal=_terminal_response(result.terminal),
        credential=result.credential,
        target_device=_registered_target_response(result.target_device),
    )


@router.get("", response_model=TerminalPageResponse)
def list_terminals(
    request: Request,
    after_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> TerminalPageResponse:
    terminals = _service(request).list_terminal_management(after_id=after_id, limit=limit)
    return TerminalPageResponse(
        items=[_terminal_management_response(item) for item in terminals],
        next_cursor=terminals[-1].terminal.terminal_id if len(terminals) == limit else None,
    )


@router.patch("/{terminal_id}/display-name", response_model=TerminalResponse)
def rename_terminal(
    terminal_id: UUID, body: RenameTerminalRequest, request: Request
) -> TerminalResponse:
    terminal = _service(request).rename_terminal(
        terminal_id=terminal_id, expected_name_version=body.expected_name_version,
        display_name=body.display_name, correlation_id=_correlation_id(request),
    )
    return _terminal_response(terminal)


@router.post("/{terminal_id}/heartbeat", response_model=TerminalResponse)
def heartbeat(
    terminal_id: UUID,
    body: HeartbeatRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> TerminalResponse:
    credential = _terminal_credential(authorization, terminal_id)
    terminal = _service(request).heartbeat(
        credential=credential,
        expected_version=body.expected_version,
        service_status=body.service_status,
        acceptance_status=body.acceptance_status,
        agent_version=body.agent_version,
        correlation_id=_correlation_id(request),
        storage=body.storage,
    )
    return _terminal_response(terminal)


@router.post(
    "/{terminal_id}/capability-profiles",
    response_model=CapabilityProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
def publish_capability(
    terminal_id: UUID,
    body: CapabilityProfileRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> CapabilityProfileResponse:
    profile = _service(request).publish_capability(
        credential=_terminal_credential(authorization, terminal_id),
        revision=body.revision,
        manifest=CapabilityManifest(
            schema_version=body.schema_version,
            protocol_version=body.protocol_version,
            agent_version=body.agent_version,
            os_name=body.os_name,
            os_version=body.os_version,
            architecture=body.architecture,
            cpu_cores=body.cpu_cores,
            memory_bytes=body.memory_bytes,
            storage_available_bytes=body.storage_available_bytes,
            accelerator_type=body.accelerator_type,
            low_resource=body.low_resource,
            provider_keys=tuple(body.provider_keys),
            details=body.details,
        ),
        correlation_id=_correlation_id(request),
    )
    return CapabilityProfileResponse(
        profile_id=profile.profile_id,
        terminal_id=profile.terminal_id,
        revision=profile.revision,
        manifest_hash=profile.manifest_hash,
        created_at=profile.created_at,
    )


@router.post("/{terminal_id}/credentials/rotate", response_model=RotatedCredentialResponse)
def rotate_credential(
    terminal_id: UUID,
    body: RotateCredentialRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> RotatedCredentialResponse:
    terminal, credential = _service(request).rotate_credential(
        credential=_terminal_credential(authorization, terminal_id),
        expected_version=body.expected_version,
        correlation_id=_correlation_id(request),
    )
    return RotatedCredentialResponse(terminal=_terminal_response(terminal), credential=credential)


@router.delete("/{terminal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_terminal(terminal_id: UUID, body: DeleteTerminalRequest, request: Request) -> Response:
    _service(request).delete_terminal(
        terminal_id=terminal_id,
        expected_version=body.expected_version,
        correlation_id=_correlation_id(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
