from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response, status

from al1s.api.maa_catalog_dependencies import (
    _application,
    _catalog,
    _commands,
    _correlation_id,
    _if_match_row_version,
)
from al1s.api.maa_catalog_schemas import (
    ApplicationListItemResponse,
    ApplicationPageResponse,
    ApplicationResponse,
    RenameApplicationRequest,
)
from al1s.maa.application_icon import MAX_UPLOAD_BYTES, normalize_icon
from al1s.maa.errors import MaaDomainError

router = APIRouter()


@router.get("/applications", response_model=ApplicationPageResponse)
def list_applications(
    request: Request,
    after_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ApplicationPageResponse:
    items = _catalog(request).list_applications(after_id=after_id, limit=limit)
    return ApplicationPageResponse(
        items=[
            ApplicationListItemResponse(**_application(item).model_dump(), script_count=count)
            for item, count in items
        ],
        next_after_id=items[-1][0].application_id if len(items) == limit else None,
    )


@router.get("/applications/{application_id}", response_model=ApplicationResponse)
def get_application(application_id: UUID, request: Request) -> ApplicationResponse:
    return _application(_catalog(request).get_application(application_id))


@router.get("/applications/{application_id}/actions")
def application_actions(application_id: UUID, request: Request) -> dict[str, str | None]:
    return _catalog(request).application_actions(application_id)


@router.get("/applications/{application_id}/delete-preview")
def application_delete_preview(application_id: UUID, request: Request) -> dict[str, Any]:
    return _catalog(request).application_delete_preview(application_id)


@router.put("/applications/{application_id}/icon", response_model=ApplicationResponse)
async def upload_application_icon(
    application_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> ApplicationResponse:
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > MAX_UPLOAD_BYTES:
            raise MaaDomainError("application_icon_size", "图标文件不得超过 2 MiB", 413)
        payload.extend(chunk)
    icon = normalize_icon(bytes(payload))
    from starlette.concurrency import run_in_threadpool

    execution = await run_in_threadpool(
        _commands(request).set_application_icon,
        application_id=application_id,
        icon_png=icon,
        expected_row_version=_if_match_row_version(if_match),
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    result = ApplicationResponse.model_validate(execution.receipt.response_body)
    response.headers["ETag"] = f'"{result.row_version}"'
    response.headers["Idempotency-Replayed"] = str(execution.replayed).lower()
    return result


@router.delete("/applications/{application_id}/icon", response_model=ApplicationResponse)
def delete_application_icon(
    application_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> ApplicationResponse:
    execution = _commands(request).set_application_icon(
        application_id=application_id,
        icon_png=None,
        expected_row_version=_if_match_row_version(if_match),
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    result = ApplicationResponse.model_validate(execution.receipt.response_body)
    response.headers["ETag"] = f'"{result.row_version}"'
    response.headers["Idempotency-Replayed"] = str(execution.replayed).lower()
    return result


@router.patch("/applications/{application_id}", response_model=ApplicationResponse)
def rename_application(
    application_id: UUID,
    body: RenameApplicationRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> ApplicationResponse:
    execution = _commands(request).rename_application(
        application_id=application_id,
        display_name=body.display_name,
        expected_row_version=_if_match_row_version(if_match),
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    result = ApplicationResponse.model_validate(execution.receipt.response_body)
    response.headers["ETag"] = f'"{result.row_version}"'
    response.headers["Idempotency-Replayed"] = str(execution.replayed).lower()
    return result


@router.delete("/applications/{application_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_application(
    application_id: UUID,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> Response:
    execution = _commands(request).delete_application(
        application_id=application_id,
        expected_row_version=_if_match_row_version(if_match),
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    return Response(
        status_code=execution.receipt.response_status,
        headers={"Idempotency-Replayed": str(execution.replayed).lower()},
    )
