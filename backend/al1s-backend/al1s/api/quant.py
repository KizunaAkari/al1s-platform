"""Internal QQ quant configuration endpoints; runtime middleware authenticates callers."""

import asyncio
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from al1s.adapters.postgres.quant_settings import QuantSettingsRecord, QuantSettingsRepository
from al1s.quant.contracts import QuantSettings
from al1s.quant.gateway import QuantGateway

router = APIRouter()


@router.get("/bots/quant/credentials")
def credential_status(request: Request) -> dict[str, bool]:
    configured = request.app.state.quant_credentials.configured()
    return {provider: provider in configured for provider in ("tushare", "gugudata")}


@router.put("/bots/quant/credentials/{provider}")
async def save_credential(
    provider: Literal["tushare", "gugudata"], request: Request
) -> dict[str, bool]:
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"secret"}:
            raise ValueError("quant_credential_invalid")
        secret = body["secret"]
        if secret is not None and not isinstance(secret, str):
            raise ValueError("quant_credential_invalid")
        await run_in_threadpool(request.app.state.quant_credentials.save, provider, secret)
    except (ValueError, TypeError) as error:
        raise HTTPException(422, "quant_credential_invalid") from error
    return {"configured": secret is not None}


@router.get("/bots/quant/status")
def get_status(request: Request) -> dict:
    process = request.app.state.quant_process
    try:
        if process is None:
            raise ValueError("quant_process_unavailable")
        return process.status()
    except (OSError, ValueError, TimeoutError) as error:
        raise HTTPException(503, "quant_status_unavailable") from error


class QuantSettingsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: QuantSettings
    commands: dict[str, str]


def _view(
    record: QuantSettingsRecord,
    gateway: QuantGateway,
    process: QuantGateway | None,
    service_id: UUID,
    worker=None,
) -> dict:
    row, mapping = gateway.applied_versions(service_id)
    try:
        process_versions = process.applied_versions(service_id) if process else None
    except Exception:
        process_versions = None
    connected = not record.settings.enabled or bool(
        worker and worker.is_quant_connected(service_id, record.settings.event_ws_url)
    )
    return {
        "desired": record.model_dump(mode="json"),
        "applied": {"row_version": row, "mapping_version": mapping},
        "process_applied": (
            {"row_version": process_versions[0], "mapping_version": process_versions[1]}
            if process_versions is not None
            else None
        ),
        "connected": connected,
        "synchronized": connected
        and record.row_version > 0
        and (row, mapping) == process_versions == (record.row_version, record.mapping_version),
    }


@router.get("/bots/quant/settings/{service_id}")
def get_settings(service_id: UUID, request: Request) -> dict:
    repository: QuantSettingsRepository = request.app.state.quant_settings
    gateway: QuantGateway = request.app.state.quant_gateway
    try:
        return _view(
            repository.get(service_id),
            gateway,
            request.app.state.quant_process,
            service_id,
            getattr(request.app.state, "quant_worker", None),
        )
    except ValueError as error:
        raise HTTPException(404, "qq_bot_not_found") from error


@router.put("/bots/quant/settings/{service_id}")
async def save_settings(
    service_id: UUID,
    data: QuantSettingsWrite,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation_id: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> dict:
    version = if_match.strip('"')
    if not version.isdigit():
        raise HTTPException(400, "invalid_row_version")
    repository: QuantSettingsRepository = request.app.state.quant_settings
    gateway: QuantGateway = request.app.state.quant_gateway
    try:
        record = await run_in_threadpool(
            repository.save,
            service_id,
            data.settings,
            data.commands,
            expected_version=int(version),
            operation_id=operation_id,
        )
    except ValueError as error:
        code = str(error)
        raise HTTPException(409 if "conflict" in code else 422, code) from error
    try:
        process = request.app.state.quant_process
        if process is None or await run_in_threadpool(process.activate, service_id, record) != (
            record.row_version,
            record.mapping_version,
        ):
            raise ValueError("quant_process_sync_unconfirmed")
        applied = gateway.activate(service_id, record)
    except Exception as error:
        raise HTTPException(503, "quant_sync_unconfirmed") from error
    if applied != (record.row_version, record.mapping_version):
        raise HTTPException(503, "quant_sync_unconfirmed")
    worker = getattr(request.app.state, "quant_worker", None)
    if record.settings.enabled:
        if worker is None or not record.settings.event_ws_url:
            raise HTTPException(503, "quant_connection_unconfirmed")
        await worker.refresh()
        for _ in range(50):
            if worker.is_quant_connected(service_id, record.settings.event_ws_url):
                break
            await asyncio.sleep(0.1)
        else:
            raise HTTPException(503, "quant_connection_unconfirmed")
    return await run_in_threadpool(_view, record, gateway, process, service_id, worker)
