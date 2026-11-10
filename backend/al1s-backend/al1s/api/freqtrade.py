from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from al1s.freqtrade.contracts import FreqtradeSettings

router = APIRouter()


class FreqtradeWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: FreqtradeSettings
    commands: dict[str, str] | None = None


def view(request: Request, service_id: UUID, record) -> dict:
    try:
        applied = request.app.state.freqtrade_client.applied(service_id)
    except (OSError, ValueError):
        applied = 0
    adapter = request.app.state.freqtrade_adapter
    connected = not record.settings.enabled or (
        adapter.applied(service_id) == record.row_version
        and request.app.state.quant_worker.is_freqtrade_connected(
            service_id, record.settings.event_ws_url
        )
    )
    return {
        "desired": record.model_dump(mode="json"),
        "applied_version": applied,
        "synchronized": record.row_version > 0 and applied == record.row_version and connected,
    }


@router.get("/bots/freqtrade/status")
def status(request: Request) -> dict:
    try:
        return request.app.state.freqtrade_client.status()
    except (OSError, ValueError) as error:
        raise HTTPException(503, "freqtrade_unavailable") from error


@router.get("/bots/freqtrade/settings/{service_id}")
def read_settings(service_id: UUID, request: Request) -> dict:
    try:
        record = request.app.state.freqtrade_repository.get(service_id)
        return view(request, service_id, record)
    except ValueError as error:
        raise HTTPException(404, "qq_bot_not_found") from error


@router.put("/bots/freqtrade/settings/{service_id}")
async def save_settings(
    service_id: UUID,
    data: FreqtradeWrite,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> dict:
    version = if_match.strip('"')
    if not version.isdigit():
        raise HTTPException(400, "invalid_row_version")
    try:
        record = await run_in_threadpool(
            request.app.state.freqtrade_repository.save,
            service_id,
            data.settings,
            data.commands,
            expected_version=int(version),
            operation_id=operation,
        )
    except ValueError as error:
        raise HTTPException(409 if "conflict" in str(error) else 422, str(error)) from error
    try:
        applied = await run_in_threadpool(
            request.app.state.freqtrade_client.activate, service_id, record
        )
        if applied != record.row_version:
            raise ValueError("freqtrade_sync_unconfirmed")
        await request.app.state.quant_worker.refresh()
    except (OSError, ValueError) as error:
        raise HTTPException(503, "freqtrade_sync_unconfirmed") from error
    return await run_in_threadpool(view, request, service_id, record)
