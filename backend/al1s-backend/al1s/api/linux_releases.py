import hmac
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, Field

from al1s.app.service_state import service_state
from al1s.execution.errors import AuthenticationError, InvalidRequestError
from al1s.releases.contracts import Release, ReleaseInput
from al1s.releases.service import ReleaseService

router = APIRouter()


def service(request: Request) -> ReleaseService:
    return service_state(request).linux_releases


@router.post("/releases/linux", status_code=201)
def create(body: ReleaseInput, request: Request) -> Release:
    return service(request).create(body)


@router.get("/releases/linux")
def listing(
    request: Request, offset: int = Query(0, ge=0, le=100000), limit: int = Query(25, ge=1, le=100)
) -> dict[str, object]:
    return {"items": service(request).store.list(offset, limit)}


@router.get("/releases/linux/{identity}")
def detail(identity: UUID, request: Request) -> Release:
    return service(request).store.get(identity)


@router.post("/releases/linux/{identity}/upload")
def upload(identity: UUID, request: Request) -> object:
    return service(request).upload(identity)


class PublishRequest(BaseModel):
    row_version: int = Field(gt=0)


@router.post("/releases/linux/{identity}/publish", status_code=202)
def publish(identity: UUID, body: PublishRequest, request: Request) -> Release:
    return service(request).store.enqueue(identity, body.row_version)


def authorize_host(request: Request, terminal_id: UUID, authorization: str | None) -> None:
    connection = service_state(request).settings.host_managers.get(terminal_id)
    supplied = authorization or ""
    if connection is None or not hmac.compare_digest(
        supplied.encode(),
        ("Bearer " + connection.token.get_secret_value()).encode(),
    ):
        raise AuthenticationError("invalid_host_credential", "Host credential is invalid")
    service_state(request).execution_resources.require_linux_terminal(terminal_id)


@router.get("/terminal/host-upgrades/{terminal_id}/releases/{identity}")
def manifest(
    terminal_id: UUID,
    identity: UUID,
    request: Request,
    response: Response,
    authorization: str | None = Header(None),
) -> Release:
    authorize_host(request, terminal_id, authorization)
    response.headers["Cache-Control"] = "no-store"
    return service(request).store.object_key(identity)[0]


@router.get("/terminal/host-upgrades/{terminal_id}/releases/{identity}/content")
def content(
    terminal_id: UUID,
    identity: UUID,
    request: Request,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    authorization: str | None = Header(None),
) -> Response:
    authorize_host(request, terminal_id, authorization)
    matched = re.fullmatch(r"bytes=(0|[1-9][0-9]*)-(0|[1-9][0-9]*)", range_header or "")
    if not matched or len(range_header or "") > 64:
        raise InvalidRequestError("release_range_required", "A bounded byte range is required")
    start, end = map(int, matched.groups())
    release, data = service(request).read_range(identity, start, end)
    return Response(
        data,
        status_code=206,
        media_type="application/x-tar",
        headers={
            "Content-Range": f"bytes {start}-{end}/{release.size_bytes}",
            "ETag": f'"sha256:{release.sha256}"',
            "Cache-Control": "no-store",
            "Accept-Ranges": "bytes",
        },
    )
