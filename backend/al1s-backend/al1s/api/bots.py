from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Header, Query, Request, Response, status
from pydantic import BaseModel, Field, SecretStr

from al1s.api.cursors import decode_timestamp_cursor, encode_timestamp_cursor
from al1s.app.service_state import service_state
from al1s.bots.discord_identity import resolve_discord_application_id
from al1s.bots.errors import BotAuthenticationError, BotDomainError
from al1s.bots.runtime_status import BotRuntimeStatus
from al1s.bots.service import BotService
from al1s.bots.types import (
    BotConfigApplicationRecord,
    BotConfigApplicationStatus,
    BotConfigVersionRecord,
    BotHealthReportRecord,
    BotHealthStatus,
    BotServiceKind,
    BotServiceRecord,
)

router = APIRouter()


@router.get("/services/{service_id}/runtime-status", response_model=BotRuntimeStatus)
def runtime_status(service_id: UUID, request: Request) -> BotRuntimeStatus:
    return service_state(request).bot_runtime.read(service_id)


class ResolveDiscordApplicationRequest(BaseModel):
    token: SecretStr


class DiscordApplicationResponse(BaseModel):
    application_id: str


@router.post("/discord/application-id", response_model=DiscordApplicationResponse)
def discord_application_id(body: ResolveDiscordApplicationRequest) -> DiscordApplicationResponse:
    return DiscordApplicationResponse(
        application_id=resolve_discord_application_id(body.token.get_secret_value())
    )


@router.get("/native-ui")
def native_ui(request: Request) -> dict[str, str | None]:
    from urllib.parse import urlsplit

    value = service_state(request).settings.llbot_webui_url
    parsed = urlsplit(value)
    safe = (
        parsed.scheme == "https"
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and parsed.path in {"", "/"}
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )
    return {"url": value if safe else None}


@router.post("/services/{service_id}/probe")
def probe_gateway(service_id: UUID, request: Request) -> dict[str, object]:

    return service_state(request).gateway_probe.apply(service_id)


class CreateBotServiceRequest(BaseModel):
    kind: BotServiceKind
    name: str = Field(min_length=1, max_length=100)


class BotServiceResponse(BaseModel):
    service_id: UUID
    kind: BotServiceKind
    name: str
    enabled: bool
    desired_config_version_id: UUID | None
    applied_config_version_id: UUID | None
    row_version: int
    created_at: datetime
    updated_at: datetime


class BotServicePageResponse(BaseModel):
    items: list[BotServiceResponse]
    next_cursor: UUID | None


class CreateBotRegistrationGrantRequest(BaseModel):
    ttl_seconds: int = Field(default=600, ge=60, le=86_400)


class BotRegistrationGrantResponse(BaseModel):
    grant_id: UUID
    registration_code: str
    expires_at: datetime


class RegisterBotWorkerRequest(BaseModel):
    registration_code: str = Field(min_length=1, max_length=1_024)


class BotWorkerCredentialResponse(BaseModel):
    identity_id: UUID
    service_id: UUID
    credential: str


class SubmitBotConfigRequest(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)
    secret: str | None = Field(default=None, max_length=4_096)
    onebot_service_id: UUID | None = None
    retain_secret_from_config_version_id: UUID | None = None


class BotConfigVersionResponse(BaseModel):
    config_version_id: UUID
    service_id: UUID
    version_no: int
    settings: dict[str, Any]
    secret_configured: bool
    onebot_service_id: UUID | None
    config_hash: str
    created_at: datetime


class BotConfigApplicationResponse(BaseModel):
    application_id: UUID
    service_id: UUID
    config_version_id: UUID
    status: BotConfigApplicationStatus
    worker_instance_id: str | None
    error_code: str | None
    error_summary: str | None
    requested_at: datetime
    completed_at: datetime | None
    row_version: int


class BotConfigApplicationPageResponse(BaseModel):
    items: list[BotConfigApplicationResponse]
    next_cursor: str | None


class SubmittedBotConfigResponse(BaseModel):
    service: BotServiceResponse
    version: BotConfigVersionResponse
    application: BotConfigApplicationResponse


class WorkerLinkedOneBotResponse(BaseModel):
    service_id: UUID
    config_version_id: UUID
    settings: dict[str, Any]
    secret: str | None


class WorkerBotConfigResponse(BaseModel):
    application_id: UUID
    application_row_version: int
    config_version_id: UUID
    version_no: int
    service_id: UUID
    settings: dict[str, Any]
    secret: str | None
    onebot_service_id: UUID | None
    linked_onebot: WorkerLinkedOneBotResponse | None
    config_hash: str


class SettleBotConfigRequest(BaseModel):
    receipt_event_id: UUID
    status: BotConfigApplicationStatus
    worker_instance_id: str = Field(min_length=1, max_length=255)
    error_code: str | None = Field(default=None, max_length=100)
    error_summary: str | None = Field(default=None, max_length=500)


class BotHeartbeatRequest(BaseModel):
    receipt_event_id: UUID
    config_version_id: UUID | None = None
    status: BotHealthStatus
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class BotHealthResponse(BaseModel):
    report_id: UUID
    receipt_event_id: UUID
    service_id: UUID
    identity_id: UUID
    config_version_id: UUID | None
    status: BotHealthStatus
    diagnostics: dict[str, Any]
    reported_at: datetime


class BotHealthPageResponse(BaseModel):
    items: list[BotHealthResponse]
    next_cursor: str | None


def _service(request: Request) -> BotService:
    return service_state(request).bot_service


def _correlation_id(request: Request) -> UUID:
    try:
        return UUID(request.state.request_id)
    except ValueError:
        return UUID(int=0)


def _resource_id(operation: str, key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"https://al1s.local/{operation}/{key}")


def _expected_version(if_match: str) -> int:
    value = if_match.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    try:
        result = int(value.strip('"'))
    except ValueError as exc:
        raise BotDomainError("invalid_if_match", "If-Match must be a positive version") from exc
    if result < 1:
        raise BotDomainError("invalid_if_match", "If-Match must be a positive version")
    return result


def _bearer(authorization: str | None) -> str:
    scheme, separator, credential = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not credential:
        raise BotAuthenticationError()
    return credential


def _service_response(item: BotServiceRecord) -> BotServiceResponse:
    return BotServiceResponse(
        service_id=item.service_id,
        kind=item.kind,
        name=item.name,
        enabled=item.enabled,
        desired_config_version_id=item.desired_config_version_id,
        applied_config_version_id=item.applied_config_version_id,
        row_version=item.row_version,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _version_response(item: BotConfigVersionRecord) -> BotConfigVersionResponse:
    return BotConfigVersionResponse(
        config_version_id=item.config_version_id,
        service_id=item.service_id,
        version_no=item.version_no,
        settings=item.settings,
        secret_configured=item.secret_id is not None,
        onebot_service_id=item.onebot_service_id,
        config_hash=item.config_hash,
        created_at=item.created_at,
    )


def _application_response(item: BotConfigApplicationRecord) -> BotConfigApplicationResponse:
    return BotConfigApplicationResponse(
        application_id=item.application_id,
        service_id=item.service_id,
        config_version_id=item.config_version_id,
        status=item.status,
        worker_instance_id=item.worker_instance_id,
        error_code=item.error_code,
        error_summary=item.error_summary,
        requested_at=item.requested_at,
        completed_at=item.completed_at,
        row_version=item.row_version,
    )


def _health_response(item: BotHealthReportRecord) -> BotHealthResponse:
    return BotHealthResponse(
        report_id=item.report_id,
        receipt_event_id=item.receipt_event_id,
        service_id=item.service_id,
        identity_id=item.identity_id,
        config_version_id=item.config_version_id,
        status=item.status,
        diagnostics=item.diagnostics,
        reported_at=item.reported_at,
    )


@router.post("/services", response_model=BotServiceResponse, status_code=status.HTTP_201_CREATED)
def create_service(
    body: CreateBotServiceRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> BotServiceResponse:
    item = _service(request).create_service(
        service_id=_resource_id("bot-service", idempotency_key),
        kind=body.kind,
        name=body.name,
        correlation_id=_correlation_id(request),
    )
    return _service_response(item)


@router.get("/services", response_model=BotServicePageResponse)
def list_services(
    request: Request,
    after_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> BotServicePageResponse:
    items = _service(request).list_services(after_id=after_id, limit=limit)
    return BotServicePageResponse(
        items=[_service_response(item) for item in items],
        next_cursor=items[-1].service_id if len(items) == limit else None,
    )


@router.get("/services/{service_id}", response_model=BotServiceResponse)
def get_service(service_id: UUID, request: Request) -> BotServiceResponse:
    return _service_response(_service(request).get_service(service_id))


@router.get(
    "/services/{service_id}/config-versions/{config_version_id}",
    response_model=BotConfigVersionResponse,
)
def get_config_version(
    service_id: UUID, config_version_id: UUID, request: Request, response: Response
) -> BotConfigVersionResponse:
    response.headers["Cache-Control"] = "no-store"
    return _version_response(_service(request).get_config_version(service_id, config_version_id))


@router.get(
    "/services/{service_id}/config-applications",
    response_model=BotConfigApplicationPageResponse,
)
def list_config_applications(
    service_id: UUID,
    request: Request,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> BotConfigApplicationPageResponse:
    before_requested_at, before_id = decode_timestamp_cursor(cursor)
    items = _service(request).list_config_applications(
        service_id,
        before_requested_at=before_requested_at,
        before_id=before_id,
        limit=limit,
    )
    return BotConfigApplicationPageResponse(
        items=[_application_response(item) for item in items],
        next_cursor=(
            encode_timestamp_cursor(items[-1].requested_at, items[-1].application_id)
            if len(items) == limit
            else None
        ),
    )


@router.get(
    "/services/{service_id}/health-reports",
    response_model=BotHealthPageResponse,
)
def list_health_reports(
    service_id: UUID,
    request: Request,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> BotHealthPageResponse:
    before_reported_at, before_id = decode_timestamp_cursor(cursor)
    items = _service(request).list_health_reports(
        service_id,
        before_reported_at=before_reported_at,
        before_id=before_id,
        limit=limit,
    )
    return BotHealthPageResponse(
        items=[_health_response(item) for item in items],
        next_cursor=(
            encode_timestamp_cursor(items[-1].reported_at, items[-1].report_id)
            if len(items) == limit
            else None
        ),
    )


@router.post(
    "/services/{service_id}/registration-grants",
    response_model=BotRegistrationGrantResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_registration_grant(
    service_id: UUID, body: CreateBotRegistrationGrantRequest, request: Request
) -> BotRegistrationGrantResponse:
    grant = _service(request).create_registration_grant(
        service_id=service_id,
        ttl=timedelta(seconds=body.ttl_seconds),
        correlation_id=_correlation_id(request),
    )
    return BotRegistrationGrantResponse(
        grant_id=grant.grant_id,
        registration_code=grant.registration_code,
        expires_at=grant.expires_at,
    )


@router.post(
    "/workers/register",
    response_model=BotWorkerCredentialResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_worker(
    body: RegisterBotWorkerRequest, request: Request
) -> BotWorkerCredentialResponse:
    worker = _service(request).register_worker(
        registration_code=body.registration_code,
        correlation_id=_correlation_id(request),
    )
    return BotWorkerCredentialResponse(
        identity_id=worker.identity_id,
        service_id=worker.service_id,
        credential=worker.credential,
    )


@router.post(
    "/services/{service_id}/config-versions",
    response_model=SubmittedBotConfigResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_config(
    service_id: UUID,
    body: SubmitBotConfigRequest,
    request: Request,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=32)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> SubmittedBotConfigResponse:
    result = _service(request).submit_config(
        service_id=service_id,
        config_version_id=_resource_id("bot-config-version", idempotency_key),
        application_id=_resource_id("bot-config-application", idempotency_key),
        expected_version=_expected_version(if_match),
        settings=body.settings,
        secret=body.secret,
        onebot_service_id=body.onebot_service_id,
        correlation_id=_correlation_id(request),
        retain_secret_from_config_version_id=body.retain_secret_from_config_version_id,
    )
    response.headers["ETag"] = f'"{result.service.row_version}"'
    return SubmittedBotConfigResponse(
        service=_service_response(result.service),
        version=_version_response(result.version),
        application=_application_response(result.application),
    )


@router.get("/worker/config", response_model=WorkerBotConfigResponse | None)
def get_worker_config(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> WorkerBotConfigResponse | Response:
    result = _service(request).get_worker_config(_bearer(authorization))
    if result is None:
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"Cache-Control": "no-store"},
        )
    response = WorkerBotConfigResponse(
        application_id=result.application.application_id,
        application_row_version=result.application.row_version,
        config_version_id=result.version.config_version_id,
        version_no=result.version.version_no,
        service_id=result.version.service_id,
        settings=result.version.settings,
        secret=result.secret,
        onebot_service_id=result.version.onebot_service_id,
        linked_onebot=(
            WorkerLinkedOneBotResponse(
                service_id=result.linked_onebot.service_id,
                config_version_id=result.linked_onebot.config_version_id,
                settings=result.linked_onebot.settings,
                secret=result.linked_onebot.secret,
            )
            if result.linked_onebot is not None
            else None
        ),
        config_hash=result.version.config_hash,
    )
    return Response(
        content=response.model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/worker/config-applications/{application_id}/result",
    response_model=BotConfigApplicationResponse,
)
def settle_config(
    application_id: UUID,
    body: SettleBotConfigRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> BotConfigApplicationResponse:
    item = _service(request).settle_config_application(
        credential=_bearer(authorization),
        application_id=application_id,
        receipt_event_id=body.receipt_event_id,
        status=body.status,
        worker_instance_id=body.worker_instance_id,
        error_code=body.error_code,
        error_summary=body.error_summary,
        correlation_id=_correlation_id(request),
    )
    return _application_response(item)


@router.post("/worker/heartbeat", response_model=BotHealthResponse)
def heartbeat(
    body: BotHeartbeatRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> BotHealthResponse:
    item = _service(request).heartbeat(
        credential=_bearer(authorization),
        receipt_event_id=body.receipt_event_id,
        config_version_id=body.config_version_id,
        status=body.status,
        diagnostics=body.diagnostics,
    )
    return _health_response(item)


@router.post(
    "/services/{service_id}/credentials/rotate",
    response_model=BotWorkerCredentialResponse,
)
def rotate_credential(
    service_id: UUID,
    request: Request,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=32)],
) -> BotWorkerCredentialResponse:
    worker = _service(request).rotate_credential(
        service_id=service_id,
        expected_version=_expected_version(if_match),
        correlation_id=_correlation_id(request),
    )
    current = _service(request).get_service(service_id)
    response.headers["ETag"] = f'"{current.row_version}"'
    return BotWorkerCredentialResponse(
        identity_id=worker.identity_id,
        service_id=worker.service_id,
        credential=worker.credential,
    )


@router.post("/services/{service_id}/disable", response_model=BotServiceResponse)
def disable_service(
    service_id: UUID,
    request: Request,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=32)],
) -> BotServiceResponse:
    item = _service(request).disable_service(
        service_id=service_id,
        expected_version=_expected_version(if_match),
        correlation_id=_correlation_id(request),
    )
    response.headers["ETag"] = f'"{item.row_version}"'
    return _service_response(item)
