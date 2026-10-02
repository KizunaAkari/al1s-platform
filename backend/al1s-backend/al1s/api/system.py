from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from al1s.app.service_state import service_state
from al1s.infrastructure.readiness import ReadinessService
from al1s.kernel.maintenance import KernelMaintenanceService
from al1s.kernel.storage_gc import StorageGcService

router = APIRouter()


class LiveResponse(BaseModel):
    status: Literal["alive"]
    service: str
    version: str
    request_id: str


class ServiceInfoResponse(BaseModel):
    service: str
    version: str
    environment: str
    modules: list[str]
    request_id: str


class ModuleResponse(BaseModel):
    id: str
    title: str
    routes: list[str]
    data_owner: str
    ui_complete: bool


@router.get('/modules', response_model=list[ModuleResponse])
def modules(request: Request) -> list[ModuleResponse]:
    return [
        ModuleResponse(
            id=module.id, title=module.title, routes=list(module.routes),
            data_owner=module.data_owner, ui_complete=module.ui_complete,
        )
        for module in service_state(request).modules.entries()
    ]


class MessagingMaintenanceResponse(BaseModel):
    pending: int
    processing: int
    dead_letter: int
    oldest_pending_at: datetime | None


class StorageMaintenanceResponse(BaseModel):
    pending_blobs: int
    quarantined_blobs: int
    pending_gc_jobs: int
    processing_gc_jobs: int
    dead_letter_gc_jobs: int
    reclaimable_blobs: int
    reclaimable_bytes: int
    automatic_interval_seconds: int = 30


class StorageGcRequestResponse(BaseModel):
    id: UUID
    status: str
    requested_at: datetime
    completed_at: datetime | None
    attempt_count: int
    claimed: int
    deleted: int
    failed: int
    stale: int
    error_type: str | None


@router.get("/health/live", response_model=LiveResponse)
def live(request: Request) -> LiveResponse:
    settings = service_state(request).settings
    return LiveResponse(
        status="alive",
        service=settings.service_name,
        version=settings.service_version,
        request_id=request.state.request_id,
    )


@router.get("/health/ready")
def ready(request: Request) -> JSONResponse:
    service: ReadinessService = service_state(request).readiness
    result = service.check()
    payload = result.as_dict(request_id=request.state.request_id)
    return JSONResponse(payload, status_code=200 if result.ready else 503)


@router.get("/info", response_model=ServiceInfoResponse)
def info(request: Request) -> ServiceInfoResponse:
    settings = service_state(request).settings
    return ServiceInfoResponse(
        service=settings.service_name,
        version=settings.service_version,
        environment=settings.environment,
        modules=[module.id for module in service_state(request).modules.entries()],
        request_id=request.state.request_id,
    )


@router.get("/maintenance/messaging", response_model=MessagingMaintenanceResponse)
def messaging_maintenance(request: Request) -> MessagingMaintenanceResponse:
    service: KernelMaintenanceService = service_state(request).maintenance
    snapshot = service.snapshot()
    return MessagingMaintenanceResponse(
        pending=snapshot.pending_outbox,
        processing=snapshot.processing_outbox,
        dead_letter=snapshot.dead_letter_outbox,
        oldest_pending_at=snapshot.oldest_pending_outbox_at,
    )


@router.get("/maintenance/storage", response_model=StorageMaintenanceResponse)
def storage_maintenance(request: Request) -> StorageMaintenanceResponse:
    service: KernelMaintenanceService = service_state(request).maintenance
    snapshot = service.snapshot()
    return StorageMaintenanceResponse(
        pending_blobs=snapshot.pending_blobs,
        quarantined_blobs=snapshot.quarantined_blobs,
        pending_gc_jobs=snapshot.pending_gc_jobs,
        processing_gc_jobs=snapshot.processing_gc_jobs,
        dead_letter_gc_jobs=snapshot.dead_letter_gc_jobs,
        reclaimable_blobs=snapshot.reclaimable_blobs,
        reclaimable_bytes=snapshot.reclaimable_bytes,
    )


@router.get("/maintenance/storage/cleanup", response_model=StorageGcRequestResponse | None)
def storage_cleanup_status(request: Request) -> StorageGcRequestResponse | None:
    service: StorageGcService = service_state(request).storage_gc
    record = service.latest()
    return StorageGcRequestResponse.model_validate(record, from_attributes=True) if record else None


@router.post("/maintenance/storage/cleanup", response_model=StorageGcRequestResponse)
def request_storage_cleanup(request: Request) -> StorageGcRequestResponse:
    service: StorageGcService = service_state(request).storage_gc
    return StorageGcRequestResponse.model_validate(service.submit(), from_attributes=True)
