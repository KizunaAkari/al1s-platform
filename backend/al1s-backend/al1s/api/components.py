import hmac
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, Field

from al1s.app.service_state import service_state
from al1s.components.bot_contracts import BotAlias
from al1s.components.contracts import (
    ComponentOperation,
    ComponentPreflight,
    ComponentRelease,
    ComponentReleaseInput,
    ComponentTarget,
    ComponentUpgradeRequest,
    InventoryPage,
    OperationPage,
    ReleasePage,
    UpgradeUnit,
)
from al1s.execution.artifact_upload_types import PresignedArtifactUpload
from al1s.execution.errors import AuthenticationError, InvalidRequestError

router = APIRouter(prefix="/components")


class PreflightRequest(BaseModel):
    target: ComponentTarget
    release_id: UUID


@router.post("/preflight", response_model=ComponentPreflight)
def preflight(body: PreflightRequest, request: Request) -> ComponentPreflight:
    return service_state(request).component_upgrades.preflight(body.target, body.release_id)


@router.post("/upgrades", response_model=ComponentOperation, status_code=202)
def start_upgrade(body: ComponentUpgradeRequest, request: Request) -> ComponentOperation:
    return service_state(request).component_upgrades.start(body)


@router.get("/operations", response_model=OperationPage)
def operations(
    request: Request,
    limit: int = Query(25, ge=1, le=25),
    cursor: UUID | None = None,
    bot_alias: BotAlias | None = None,
) -> OperationPage:
    store = service_state(request).component_upgrades.store
    return (
        store.list_operations(limit, cursor, f"bot:{bot_alias}")
        if bot_alias
        else store.list_operations(limit, cursor)
    )


@router.get("/operations/{identity}", response_model=ComponentOperation)
def operation(identity: UUID, request: Request) -> ComponentOperation:
    return service_state(request).component_upgrades.get(identity)


@router.post("/operations/{identity}/reconcile", response_model=ComponentOperation)
def reconcile(identity: UUID, request: Request) -> ComponentOperation:
    return service_state(request).component_upgrades.reconcile(identity)


@router.get("/internal/operations/{identity}")
def authorized_operation(
    identity: UUID, request: Request, authorization: str | None = Header(None)
) -> dict[str, object]:
    authorize_host(request, authorization)
    service = service_state(request).component_upgrades
    operation = service.store.get_operation(identity)
    release = service.store.get_release(operation.release_id)
    return {
        "operation": operation.model_dump(mode="json"),
        "release": release.model_dump(mode="json"),
    }


@router.get("/inventory", response_model=InventoryPage)
def inventory(
    request: Request, limit: int = Query(20, ge=1, le=20), cursor: UUID | None = None
) -> InventoryPage:
    return service_state(request).component_inventory.list(limit, cursor)


@router.get("/releases", response_model=ReleasePage)
def listing(
    request: Request,
    component: UpgradeUnit,
    limit: int = Query(25, ge=1, le=25),
    cursor: UUID | None = None,
) -> ReleasePage:
    return service_state(request).component_releases.list(component, limit, cursor)


@router.post("/releases", response_model=ComponentRelease, status_code=201)
def create(body: ComponentReleaseInput, request: Request) -> ComponentRelease:
    return service_state(request).component_releases.create(body)


@router.post("/releases/{identity}/upload")
def upload(identity: UUID, request: Request) -> PresignedArtifactUpload:
    return service_state(request).component_releases.upload(identity)


class PublishRequest(BaseModel):
    row_version: int = Field(ge=1)


@router.post("/releases/{identity}/publish", response_model=ComponentRelease, status_code=202)
def publish(identity: UUID, body: PublishRequest, request: Request) -> ComponentRelease:
    return service_state(request).component_releases.publish(identity, body.row_version)


def authorize_host(request: Request, authorization: str | None) -> None:
    secret = service_state(request).settings.component_host
    if secret is None or not hmac.compare_digest(
        (authorization or "").encode(), ("Bearer " + secret.token.get_secret_value()).encode()
    ):
        raise AuthenticationError(
            "invalid_component_host", "Component executor credential is invalid"
        )


@router.get("/internal/releases/{identity}")
def internal_manifest(
    identity: UUID, request: Request, authorization: str | None = Header(None)
) -> ComponentRelease:
    authorize_host(request, authorization)
    return service_state(request).component_releases.store.host_manifest(identity)


@router.get("/internal/releases/{identity}/content")
def internal_content(
    identity: UUID,
    request: Request,
    authorization: str | None = Header(None),
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    authorize_host(request, authorization)
    matched = re.fullmatch(r"bytes=(0|[1-9][0-9]*)-(0|[1-9][0-9]*)", range_header or "")
    if not matched:
        raise InvalidRequestError("invalid_component_range", "Use a bounded byte range")
    start, end = map(int, matched.groups())
    release, content = service_state(request).component_releases.read_range(
        identity, start, end, True
    )
    return Response(
        content,
        status_code=206,
        media_type="application/x-tar",
        headers={
            "Content-Range": f"bytes {start}-{end}/{release.size_bytes}",
            "ETag": f'"sha256:{release.sha256}"',
            "Cache-Control": "no-store",
        },
    )
