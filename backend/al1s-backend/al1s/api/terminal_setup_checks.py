from dataclasses import asdict
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from al1s.api.terminal_auth import authenticated_terminal_id
from al1s.app.service_state import service_state
from al1s.execution.terminal_setup_checks import SetupFacts

router = APIRouter()


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: UUID


class Facts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secure_lock: StrictBool
    provider_ready: StrictBool
    battery_exempt: StrictBool
    notifications_allowed: StrictBool
    wireless_recovery_enabled: StrictBool
    manufacturer_policy: Literal["unverifiable"] = "unverifiable"


class Complete(Claim):
    facts: Facts


@router.get("/terminal/setup-checks/pending", response_model=None)
def pending(request: Request, authorization: str | None = Header(None)) -> dict[str, object]:
    terminal = authenticated_terminal_id(request, authorization)
    result = service_state(request).terminal_setup_checks.request(terminal)
    return {"check": asdict(result) if result else None}


@router.post("/terminal/setup-checks/{check_id}/claim", response_model=None)
def claim(
    check_id: UUID, body: Claim, request: Request, authorization: str | None = Header(None)
) -> dict[str, object]:
    terminal = authenticated_terminal_id(request, authorization)
    return asdict(
        service_state(request).terminal_setup_checks.claim(terminal, check_id, body.instance_id)
    )


@router.post("/terminal/setup-checks/{check_id}/complete", response_model=None)
def complete(
    check_id: UUID, body: Complete, request: Request, authorization: str | None = Header(None)
) -> dict[str, object]:
    terminal = authenticated_terminal_id(request, authorization)
    return asdict(
        service_state(request).terminal_setup_checks.complete(
            terminal, check_id, body.instance_id, SetupFacts(**body.facts.model_dump())
        )
    )


@router.post("/terminals/{terminal_id}/setup-check", response_model=None)
def request_check(
    terminal_id: UUID, request: Request, authorization: str | None = Header(None)
) -> dict[str, object] | None:
    if authorization is not None:
        raise HTTPException(403, "Operator context required")
    result = service_state(request).terminal_setup_checks.request(terminal_id, manual=True)
    return asdict(result) if result else None


@router.get("/terminals/{terminal_id}/setup-check", response_model=None)
def latest_check(
    terminal_id: UUID, request: Request, authorization: str | None = Header(None)
) -> dict[str, object] | None:
    if authorization is not None:
        raise HTTPException(403, "Operator context required")
    result = service_state(request).terminal_setup_checks.latest(terminal_id)
    return asdict(result) if result else None
