from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from al1s.trading_admin.contracts import Market, TradingActionWrite, TradingOwnerWrite

router = APIRouter()


def _error(error: Exception) -> HTTPException:
    code = str(error)
    if code in {"trading_owner_required", "trading_owner_busy", "trading_owner_conflict",
                "trading_owner_changed", "trading_operation_pending", "trading_operation_conflict", "idempotency_conflict"}:
        return HTTPException(409, code)
    if code in {"qq_bot_not_found", "trading_operation_not_found"}:
        return HTTPException(404, code)
    if code in {"trading_action_invalid", "quant_qq_user_invalid", "invalid_idempotency_key"}:
        return HTTPException(422, code)
    return HTTPException(503, "trading_service_unavailable")


@router.get("/bots/trading/account-owner")
def owner(request: Request):
    return request.app.state.trading_admin_repository.owner()


@router.put("/bots/trading/account-owner")
async def set_owner(data: TradingOwnerWrite, request: Request,
                    if_match: Annotated[str, Header(alias="If-Match", max_length=20)]):
    version = if_match.strip('"')
    if not version.isdigit():
        raise HTTPException(400, "invalid_row_version")
    try:
        return await run_in_threadpool(request.app.state.trading_admin_repository.set_owner, data.qq_id, int(version))
    except ValueError as error:
        raise _error(error) from error


@router.get("/bots/trading/{service_id}/{market}/{view}")
async def account(service_id: UUID, market: Market, view: Literal["account", "positions", "orders"], request: Request,
                  page: Annotated[int, Query(ge=1, le=1000)] = 1):
    try:
        return await run_in_threadpool(request.app.state.trading_admin.query, service_id, market,
            {"account": "balance", "positions": "positions", "orders": "trades"}[view], page)
    except (OSError, ValueError, TimeoutError) as error:
        raise _error(error) from error


@router.post("/bots/trading/{service_id}/{market}/actions")
async def submit(service_id: UUID, market: Market, data: TradingActionWrite, request: Request,
                 operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
    try:
        return await run_in_threadpool(request.app.state.trading_admin.submit, service_id, market, data, operation)
    except (OSError, ValueError, TimeoutError) as error:
        raise _error(error) from error


@router.get("/bots/trading/{service_id}/{market}/actions/{operation_id}")
async def operation(service_id: UUID, market: Market, operation_id: UUID, request: Request):
    try:
        return await run_in_threadpool(request.app.state.trading_admin.operation, service_id, market, operation_id)
    except ValueError as error:
        raise _error(error) from error
