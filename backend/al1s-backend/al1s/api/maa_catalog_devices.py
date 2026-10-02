from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response

from al1s.api.maa_catalog_dependencies import (
    _correlation_id,
)
from al1s.api.maa_catalog_schemas import (
    ApplicableApplicationResponse,
    ApplicationDeviceRequest,
    ApplicationDeviceResponse,
    CreateEditorScriptRequest,
    EditorScriptCreatedResponse,
)
from al1s.app.service_state import service_state
from al1s.maa.app_icon_probe import fetch_application_icon
from al1s.maa.foreground_probe import probe_foreground_package

router = APIRouter()


@router.get(
    "/applications/{application_id}/devices", response_model=list[ApplicationDeviceResponse]
)
def list_application_devices(
    application_id: UUID, request: Request
) -> list[ApplicationDeviceResponse]:
    service = service_state(request).maa_applicability
    return [
        ApplicationDeviceResponse(
            application_id=item.application_id, device_id=item.device_id, created_at=item.created_at
        )
        for item in service.list_devices(application_id)
    ]


@router.get("/devices/{device_id}/applications", response_model=list[ApplicableApplicationResponse])
def device_applications(device_id: UUID, request: Request) -> list[ApplicableApplicationResponse]:
    service = service_state(request).maa_applicability
    return [
        ApplicableApplicationResponse.model_validate(item)
        for item in service.list_applications(device_id)
    ]


@router.post("/applications/{application_id}/devices", response_model=ApplicationDeviceResponse)
def bind_application_device(
    application_id: UUID,
    body: ApplicationDeviceRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> ApplicationDeviceResponse:
    service = service_state(request).maa_applicability
    result = service.bind(
        application_id=application_id, device_id=body.device_id, idempotency_key=idempotency_key
    )
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return ApplicationDeviceResponse(
        application_id=application_id,
        device_id=body.device_id,
        created_at=result.receipt.created_at,
    )


@router.delete("/applications/{application_id}/devices/{device_id}")
def unbind_application_device(
    application_id: UUID,
    device_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, str]:
    service = service_state(request).maa_applicability
    result = service.unbind(
        application_id=application_id, device_id=device_id, idempotency_key=idempotency_key
    )
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return {"application_id": str(application_id), "device_id": str(device_id)}


@router.get("/editor/foreground")
async def editor_foreground(device_id: UUID, request: Request) -> dict[str, str]:
    package = await probe_foreground_package(service_state(request).editor_sessions, device_id)
    return {"package_name": package}


@router.post("/editor/scripts", response_model=EditorScriptCreatedResponse, status_code=201)
async def create_editor_script(
    body: CreateEditorScriptRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> EditorScriptCreatedResponse:
    package = await probe_foreground_package(service_state(request).editor_sessions, body.device_id)
    creator = service_state(request).maa_editor_creation
    from starlette.concurrency import run_in_threadpool

    icon: bytes | None = None
    if await run_in_threadpool(creator.needs_icon, body.device_id, package):
        icon = await fetch_application_icon(
            service_state(request).editor_sessions,
            body.device_id,
            package,
        )
    execution = await run_in_threadpool(
        creator.create,
        device_id=body.device_id,
        package_name=package,
        name=body.name,
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
        icon_png=icon,
    )
    response.headers["Idempotency-Replayed"] = str(execution.replayed).lower()
    return EditorScriptCreatedResponse.model_validate(execution.receipt.response_body)
