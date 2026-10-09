from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from al1s.api.terminal_auth import authenticated_terminal_id
from al1s.app.service_state import service_state
from al1s.execution.device_path_service import PathObservation

router = APIRouter()


@router.get("/target-devices/{device_id}/paths")
def paths(
    device_id: UUID, request: Request, authorization: str | None = Header(None)
) -> dict[str, object]:
    if authorization is not None:
        raise HTTPException(403, "Operator context required")
    return service_state(request).device_paths.view(device_id)


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: UUID
    instance_id: UUID
    control_ready: bool
    release_generation: int | None = Field(default=None, ge=1)
    released_previous_instance_id: UUID | None = None


class ObservationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Observation] = Field(min_length=1, max_length=100)


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_terminal_id: UUID
    expected_generation: int = Field(ge=1)


@router.post("/target-devices/{device_id}/paths/handoff")
def request_handoff(
    device_id: UUID, body: HandoffRequest, request: Request,
    authorization: str | None = Header(None),
) -> dict[str, object]:
    if authorization is not None:
        raise HTTPException(403, "Operator context required")
    return service_state(request).device_paths.request_handoff(
        device_id, body.target_terminal_id, body.expected_generation
    )


@router.post("/terminal/device-paths/observations")
def observe(
    body: ObservationBatch, request: Request, authorization: str | None = Header(None)
) -> dict[str, list[dict[str, object]]]:
    terminal = authenticated_terminal_id(request, authorization)
    return {
        "items": service_state(request).device_paths.observe(
            terminal,
            [PathObservation(**item.model_dump()) for item in body.items],
        )
    }
