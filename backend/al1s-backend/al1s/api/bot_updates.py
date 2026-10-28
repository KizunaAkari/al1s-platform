from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request

from al1s.app.service_state import service_state
from al1s.components.bot_candidates import QqCandidateService
from al1s.components.bot_contracts import BotAlias, BotUpdateState
from al1s.components.bot_sources import OfficialQqSource
from al1s.components.contracts import ComponentTarget, ReleasePage

router = APIRouter(prefix="/bots/updates")


def candidate_service(request: Request) -> QqCandidateService:
    services = service_state(request)
    settings = services.settings
    return QqCandidateService(
        services.component_releases.store,
        OfficialQqSource(),
        settings.qq_runtime_artifact_digest,
        settings.qq_runtime_version,
    )


@router.get("/{alias}", response_model=BotUpdateState)
def state(alias: BotAlias, request: Request) -> BotUpdateState:
    row = service_state(request).component_inventory.get(
        ComponentTarget(kind="bot", bot_alias=alias), alias + "-bot"
    )
    if row is None:
        raise HTTPException(503, detail="bot_version_unavailable")
    return BotUpdateState(alias=alias, current=row, runtime_version=row.runtime_version)


@router.post("/qq/check", response_model=ReleasePage)
def check(request: Request) -> ReleasePage:
    return ReleasePage(items=candidate_service(request).check())


@router.get("/{alias}/versions", response_model=ReleasePage)
def versions(
    alias: BotAlias,
    request: Request,
    limit: int = Query(5, ge=1, le=25),
    cursor: UUID | None = None,
) -> ReleasePage:
    return service_state(request).component_releases.list_bot(alias, limit, cursor)


@router.post("/qq/candidates/{identity}/prepare")
def prepare(identity: UUID, request: Request, if_match: str = Header()) -> object:
    if not if_match.isdecimal():
        raise HTTPException(400, detail="row_version_required")
    return candidate_service(request).prepare(identity, int(if_match))
