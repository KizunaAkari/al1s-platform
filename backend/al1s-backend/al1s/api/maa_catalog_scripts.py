from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from al1s.api.maa_catalog_dependencies import (
    _catalog,
    _commands,
    _correlation_id,
    _if_match_row_version,
    _publication,
    _script,
    _script_commands,
    _script_version,
)
from al1s.api.maa_catalog_schemas import (
    RenameScriptRequest,
    ReorderScriptRequest,
    SaveScriptDocumentRequest,
    ScriptPageResponse,
    ScriptResponse,
    ScriptVersionDetailResponse,
    ScriptVersionPageResponse,
    StaticCheckRequest,
    StaticCheckResponse,
)

router = APIRouter()


@router.get("/editor/drafts", response_model=ScriptPageResponse)
def list_editor_drafts(
    request: Request,
    device_id: Annotated[UUID, Query()],
    after_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ScriptPageResponse:
    items = _catalog(request).list_editor_drafts(
        device_id=device_id,
        after_id=after_id,
        limit=limit,
    )
    return ScriptPageResponse(
        items=[_script(item) for item in items],
        next_after_id=items[-1].script_id if len(items) == limit else None,
    )


@router.get("/scripts", response_model=ScriptPageResponse)
def list_scripts(
    request: Request,
    application_id: Annotated[UUID | None, Query()] = None,
    after_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ScriptPageResponse:
    items = _catalog(request).list_scripts(
        application_id=application_id,
        after_id=after_id,
        limit=limit,
    )
    return ScriptPageResponse(
        items=[_script(item) for item in items],
        next_after_id=items[-1].script_id if len(items) == limit else None,
    )


@router.get("/applications/{application_id}/library-scripts", response_model=ScriptPageResponse)
def list_library_scripts(
    application_id: UUID,
    request: Request,
    after_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ScriptPageResponse:
    items = _catalog(request).list_library_scripts(
        application_id=application_id,
        after_id=after_id,
        limit=limit,
    )
    return ScriptPageResponse(
        items=[_script(item) for item in items],
        next_after_id=items[-1].script_id if len(items) == limit else None,
    )


@router.put("/applications/{application_id}/library-scripts/order", status_code=204)
def reorder_library_script(
    application_id: UUID,
    body: ReorderScriptRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> Response:
    execution = _commands(request).reorder_script(
        application_id=application_id,
        script_id=body.script_id,
        target_id=body.target_id,
        placement=body.placement,
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    return Response(
        status_code=execution.receipt.response_status,
        headers={
            "Idempotency-Replayed": str(execution.replayed).lower(),
        },
    )


@router.get("/scripts/{script_id}", response_model=ScriptResponse)
def get_script(script_id: UUID, request: Request, response: Response) -> ScriptResponse:
    result = _script(_catalog(request).get_script(script_id))
    response.headers["ETag"] = f'"{result.row_version}"'
    return result


@router.patch("/scripts/{script_id}", response_model=ScriptResponse)
def rename_script(
    script_id: UUID,
    body: RenameScriptRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> ScriptResponse:
    execution = _script_commands(request).rename_script(
        script_id=script_id,
        name=body.name,
        expected_row_version=_if_match_row_version(if_match),
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    result = ScriptResponse.model_validate(execution.receipt.response_body)
    response.headers["ETag"] = f'"{result.row_version}"'
    response.headers["Idempotency-Replayed"] = str(execution.replayed).lower()
    return result


@router.get("/scripts/{script_id}/metadata-actions")
def script_metadata_actions(script_id: UUID, request: Request) -> dict[str, str | int | None]:
    script = _catalog(request).get_script(script_id)
    return {"rename": None, "row_version": script.row_version}


@router.get("/scripts/{script_id}/referrers")
def script_referrers(
    script_id: UUID,
    request: Request,
    after_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    items = _catalog(request).script_referrers(script_id, after_id=after_id, limit=limit + 1)
    return {
        "items": items[:limit],
        "next_after_id": items[limit - 1]["script_id"] if len(items) > limit else None,
    }


@router.delete("/scripts/{script_id}")
def delete_script(
    script_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> dict[str, Any]:
    result = _script_commands(request).delete_script(
        script_id=script_id,
        expected_row_version=_if_match_row_version(if_match),
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return result.receipt.response_body


@router.put("/scripts/{script_id}/document", response_model=ScriptResponse)
def save_script_document(
    script_id: UUID,
    body: SaveScriptDocumentRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> ScriptResponse:
    execution = _script_commands(request).save_document(
        script_id=script_id,
        expected_row_version=_if_match_row_version(if_match),
        manifest=body.manifest,
        device_id=body.device_id,
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    result = ScriptResponse.model_validate(execution.receipt.response_body["script"])
    response.headers["ETag"] = f'"{result.row_version}"'
    response.headers["Idempotency-Replayed"] = str(execution.replayed).lower()
    return result


@router.post("/scripts/{script_id}/static-check", response_model=StaticCheckResponse)
def check_script_candidate(
    script_id: UUID,
    body: StaticCheckRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> StaticCheckResponse:
    receipt = _publication(request).validate_candidate(
        script_id=script_id,
        candidate_version_id=body.candidate_version_id,
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    return StaticCheckResponse(
        receipt_id=receipt.receipt_id,
        kind=receipt.kind.value,
        status=receipt.status.value,
        executor_version=receipt.executor_version,
        created_at=receipt.created_at,
        issues=receipt.diagnostic.get("issues", []),
    )


@router.get("/scripts/{script_id}/versions", response_model=ScriptVersionPageResponse)
def list_script_versions(
    script_id: UUID,
    request: Request,
    after_revision: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    newest_first: Annotated[bool, Query()] = False,
) -> ScriptVersionPageResponse:
    items = _catalog(request).list_script_versions(
        script_id=script_id,
        after_revision=after_revision,
        limit=limit,
        newest_first=newest_first,
    )
    return ScriptVersionPageResponse(
        items=[_script_version(item) for item in items],
        next_after_revision=items[-1].revision if len(items) == limit else None,
    )


@router.get(
    "/scripts/{script_id}/versions/{script_version_id}",
    response_model=ScriptVersionDetailResponse,
)
def get_script_version(
    script_id: UUID, script_version_id: UUID, request: Request
) -> ScriptVersionDetailResponse:
    item = _catalog(request).get_script_version(
        script_id=script_id, script_version_id=script_version_id
    )
    summary = _script_version(item)
    return ScriptVersionDetailResponse(
        **summary.model_dump(),
        script_id=item.script_id,
        manifest=item.manifest,
    )
