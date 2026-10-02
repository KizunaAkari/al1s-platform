from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response, status

from al1s.api.maa_catalog_dependencies import (
    _catalog,
    _correlation_id,
    _if_match_row_version,
    _process_modules,
    _strategy,
    _strategy_commands,
    _strategy_module,
    _strategy_version,
)
from al1s.api.maa_catalog_schemas import (
    CreateStrategyRequest,
    RenameStrategyRequest,
    StrategyDefinitionRequest,
    StrategyEditResponse,
    StrategyMutationResponse,
    StrategyPageResponse,
    StrategyResponse,
    StrategyVersionDetailResponse,
    StrategyVersionPageResponse,
)

router = APIRouter()


@router.get("/strategies", response_model=StrategyPageResponse)
def list_strategies(
    request: Request,
    application_id: Annotated[UUID | None, Query()] = None,
    after_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> StrategyPageResponse:
    items = _catalog(request).list_strategies(
        application_id=application_id,
        after_id=after_id,
        limit=limit,
    )
    return StrategyPageResponse(
        items=[_strategy(item) for item in items],
        next_after_id=items[-1].strategy_id if len(items) == limit else None,
    )


@router.post(
    "/strategies",
    response_model=StrategyMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_strategy(
    body: CreateStrategyRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> StrategyMutationResponse:
    execution = _strategy_commands(request).create_strategy(
        application_id=body.application_id,
        name=body.name,
        start_script_id=body.start_script_id,
        process_modules=_process_modules(body.process_modules),
        end_script_id=body.end_script_id,
        start_wait_after_ms=body.start_wait_after_ms,
        default_parameters=body.default_parameters,
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    result = StrategyMutationResponse.model_validate(execution.receipt.response_body)
    response.headers["ETag"] = f'"{result.strategy.row_version}"'
    response.headers["Idempotency-Replayed"] = str(execution.replayed).lower()
    return result


@router.get("/strategies/{strategy_id}", response_model=StrategyResponse)
def get_strategy(strategy_id: UUID, request: Request, response: Response) -> StrategyResponse:
    result = _strategy(_catalog(request).get_strategy(strategy_id))
    response.headers["ETag"] = f'"{result.row_version}"'
    return result


@router.get("/strategies/{strategy_id}/edit-definition", response_model=StrategyEditResponse)
def get_strategy_edit_definition(strategy_id: UUID, request: Request) -> StrategyEditResponse:
    strategy, definition = _catalog(request).get_strategy_edit_definition(strategy_id)
    return StrategyEditResponse(
        strategy=_strategy(strategy),
        definition=StrategyDefinitionRequest.model_validate(definition),
    )


@router.patch("/strategies/{strategy_id}", response_model=StrategyResponse)
def rename_strategy(
    strategy_id: UUID,
    body: RenameStrategyRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> StrategyResponse:
    execution = _strategy_commands(request).rename_strategy(
        strategy_id=strategy_id,
        name=body.name,
        expected_row_version=_if_match_row_version(if_match),
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    result = StrategyResponse.model_validate(execution.receipt.response_body)
    response.headers["ETag"] = f'"{result.row_version}"'
    response.headers["Idempotency-Replayed"] = str(execution.replayed).lower()
    return result


@router.delete("/strategies/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_strategy(
    strategy_id: UUID,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> Response:
    execution = _strategy_commands(request).delete_strategy(
        strategy_id=strategy_id,
        expected_row_version=_if_match_row_version(if_match),
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    return Response(
        status_code=execution.receipt.response_status,
        headers={"Idempotency-Replayed": str(execution.replayed).lower()},
    )


@router.put("/strategies/{strategy_id}/definition", response_model=StrategyResponse)
def save_strategy_definition(
    strategy_id: UUID,
    body: StrategyDefinitionRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> StrategyResponse:
    execution = _strategy_commands(request).save_strategy(
        strategy_id=strategy_id,
        expected_row_version=_if_match_row_version(if_match),
        start_script_id=body.start_script_id,
        process_modules=_process_modules(body.process_modules),
        end_script_id=body.end_script_id,
        start_wait_after_ms=body.start_wait_after_ms,
        default_parameters=body.default_parameters,
        idempotency_key=idempotency_key,
        correlation_id=_correlation_id(request),
    )
    result = StrategyResponse.model_validate(execution.receipt.response_body["strategy"])
    response.headers["ETag"] = f'"{result.row_version}"'
    response.headers["Idempotency-Replayed"] = str(execution.replayed).lower()
    return result


@router.get(
    "/strategies/{strategy_id}/versions",
    response_model=StrategyVersionPageResponse,
)
def list_strategy_versions(
    strategy_id: UUID,
    request: Request,
    after_revision: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> StrategyVersionPageResponse:
    items = _catalog(request).list_strategy_versions(
        strategy_id=strategy_id,
        after_revision=after_revision,
        limit=limit,
    )
    return StrategyVersionPageResponse(
        items=[_strategy_version(item) for item in items],
        next_after_revision=items[-1].revision if len(items) == limit else None,
    )


@router.get(
    "/strategies/{strategy_id}/versions/{strategy_version_id}",
    response_model=StrategyVersionDetailResponse,
)
def get_strategy_version(
    strategy_id: UUID, strategy_version_id: UUID, request: Request
) -> StrategyVersionDetailResponse:
    version, modules = _catalog(request).get_strategy_version(
        strategy_id=strategy_id,
        strategy_version_id=strategy_version_id,
    )
    summary = _strategy_version(version)
    return StrategyVersionDetailResponse(
        **summary.model_dump(),
        strategy_id=version.strategy_id,
        manifest=version.manifest,
        modules=[_strategy_module(item) for item in modules],
    )
