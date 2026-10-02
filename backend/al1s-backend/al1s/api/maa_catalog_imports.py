from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from al1s.api.maa_catalog_dependencies import (
    _catalog,
    _correlation_id,
    _import_batch,
    _import_commands,
    _import_item,
)
from al1s.api.maa_catalog_schemas import (
    ImportArchiveRequest,
    ImportBatchResponse,
    ImportItemPageResponse,
)

router = APIRouter()


@router.get("/imports/{batch_id}", response_model=ImportBatchResponse)
def get_import(batch_id: UUID, request: Request) -> ImportBatchResponse:
    return _import_batch(_catalog(request).get_import(batch_id))


@router.post("/imports", response_model=ImportBatchResponse)
def import_archive(
    body: ImportArchiveRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> ImportBatchResponse:
    execution = _import_commands(request).import_ready_blob(
        archive_blob_id=body.archive_blob_id,
        target_applications=body.target_applications,
        confirmed_overwrites=body.confirmed_overwrites,
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    result = ImportBatchResponse.model_validate(execution.receipt.response_body)
    response.status_code = execution.receipt.response_status
    response.headers["Idempotency-Replayed"] = str(execution.replayed).lower()
    return result


@router.get("/imports/{batch_id}/items", response_model=ImportItemPageResponse)
def list_import_items(
    batch_id: UUID,
    request: Request,
    after_ordinal: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ImportItemPageResponse:
    items = _catalog(request).list_import_items(
        batch_id=batch_id,
        after_ordinal=after_ordinal,
        limit=limit,
    )
    return ImportItemPageResponse(
        items=[_import_item(item) for item in items],
        next_after_ordinal=items[-1].archive_ordinal if len(items) == limit else None,
    )
