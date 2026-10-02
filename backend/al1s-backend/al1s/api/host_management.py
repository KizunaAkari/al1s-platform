from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, Response

from al1s.app.service_state import service_state
from al1s.execution.errors import ExecutionDomainError
from al1s.execution.host_management import (
    CancelMaintenanceRequest,
    HostManagerClient,
    RecoveryRequest,
    RestartRequest,
    UpgradeRequest,
)

router = APIRouter(prefix="/terminals/{terminal_id}/maintenance")


@router.post("/cancel")
def cancel(
    terminal_id: UUID, body: CancelMaintenanceRequest, request: Request, response: Response,
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    service_state(request).execution_resources.require_linux_terminal(terminal_id)
    service = service_state(request).host_maintenance
    return service.cancel(terminal_id, body).response()


@router.post("/upgrades", status_code=202)
def upgrade(
    terminal_id: UUID, body: UpgradeRequest, request: Request, response: Response
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    service_state(request).execution_resources.require_linux_terminal(terminal_id)
    service_state(request).linux_releases.store.object_key(body.release_id)
    service = service_state(request).host_maintenance
    return service.submit(terminal_id, body).response()


@router.post("/recovery")
def recovery(
    terminal_id: UUID, body: RecoveryRequest, request: Request, response: Response
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    return client(request, terminal_id).request("/v1/upgrades/recover", body.model_dump())


def client(request: Request, terminal_id: UUID) -> HostManagerClient:
    service_state(request).execution_resources.require_linux_terminal(terminal_id)
    connection = service_state(request).settings.host_managers.get(terminal_id)
    if connection is None:
        raise ExecutionDomainError(
            "host_management_not_configured", "Independent host management is not configured", 503
        )
    return HostManagerClient(connection)


@router.get("/health")
def health(terminal_id: UUID, request: Request, response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    return client(request, terminal_id).request("/v1/health")


@router.get("/logs")
def logs(terminal_id: UUID, request: Request, response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    return client(request, terminal_id).request("/v1/logs")


@router.get("/impact")
def impact(terminal_id: UUID, request: Request, response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    service_state(request).execution_resources.require_linux_terminal(terminal_id)
    rows = service_state(request).execution_scheduling.maintenance_impact(terminal_id)
    return {"items": rows[:100], "truncated": len(rows) > 100}


@router.post("/commands", status_code=202)
def restart(
    terminal_id: UUID, body: RestartRequest, request: Request, response: Response
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    service_state(request).execution_resources.require_linux_terminal(terminal_id)
    service = service_state(request).host_maintenance
    return service.submit(terminal_id, body).response()


@router.get("/commands/{command_id}")
def command(
    terminal_id: UUID, command_id: UUID, request: Request, response: Response
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    service_state(request).execution_resources.require_linux_terminal(terminal_id)
    service = service_state(request).host_maintenance
    return service.get(terminal_id, command_id).response()


@router.get("/commands")
def latest(terminal_id: UUID, request: Request, response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    service_state(request).execution_resources.require_linux_terminal(terminal_id)
    service = service_state(request).host_maintenance
    item = service.latest(terminal_id)
    return {"item": item.response() if item else None}
