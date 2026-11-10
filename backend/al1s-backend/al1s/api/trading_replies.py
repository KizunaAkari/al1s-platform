from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from al1s.trading_replies.catalog import COMMON, arguments_example, catalogue, words
from al1s.trading_replies.render import allowed, preview, text

router = APIRouter()
Engine = Literal["quant", "freqtrade"]


class CommandWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword: str = Field(min_length=1, max_length=32)
    templates: dict[str, str] = Field(default_factory=dict, max_length=16)


class PreviewWrite(CommandWrite):
    variant: str = Field(min_length=1, max_length=32)


def synchronized(request, service_id, engine, record):
    try:
        worker = getattr(request.app.state, "quant_worker", None)
        connected = not record.settings.enabled or bool(
            worker
            and (
                worker.is_quant_connected(service_id, record.settings.event_ws_url)
                if engine == "quant"
                else worker.is_freqtrade_connected(service_id, record.settings.event_ws_url)
            )
        )
        if engine == "quant":
            expected = record.row_version, record.mapping_version
            return (
                record.row_version > 0
                and connected
                and (
                    request.app.state.quant_gateway.applied_versions(service_id) == expected
                    and request.app.state.quant_process.applied_versions(service_id) == expected
                )
            )
        return (
            record.row_version > 0
            and connected
            and (request.app.state.freqtrade_adapter.applied(service_id) == record.row_version)
        )
    except Exception:
        return False


def view(request, service_id, engine, record):
    items = []
    mapping = {**words(engine), **record.commands}
    for action, item in catalogue(engine).items():
        variants = []
        for key, spec in item["variants"].items():
            samples = {
                **COMMON,
                **spec["sample"],
                **{k + "_command": v for k, v in mapping.items()},
                "default_reply": "当前结果的完整默认回复",
                "market_label": "A股"
                if engine == "quant"
                else ("合约" if item["group"] == "合约" else "现货"),
                "quote_currency": "元" if engine == "quant" else "USDT",
                "trigger": mapping[action],
                "initial_cash": text(
                    record.settings.futures_initial_cash
                    if engine == "freqtrade" and item["group"] == "合约"
                    else record.settings.initial_cash
                ),
            }
            variants.append(
                dict(
                    id=key,
                    label=spec["label"],
                    default_template=spec["default_template"],
                    variables=[
                        dict(key=name, label=label, example=str(samples.get(name, "示例数据")))
                        for name, label in sorted(allowed(engine, action, key).items())
                    ],
                )
            )
        items.append(
            dict(
                id=action,
                label=item["label"],
                group=item["group"],
                description=item["description"],
                usage=(mapping[action] + " " + arguments_example(engine, action)).strip(),
                keyword=mapping[action],
                default_keyword=words(engine)[action],
                templates=record.reply_templates.get(action, {}),
                variants=variants,
            )
        )
    return dict(
        engine=engine,
        row_version=record.row_version,
        mapping_version=getattr(record, "mapping_version", record.row_version),
        synchronized=synchronized(request, service_id, engine, record),
        commands=items,
    )


async def activate(request, service_id, engine, record):
    try:
        if engine == "quant":
            result = await run_in_threadpool(
                request.app.state.quant_process.activate, service_id, record
            )
            if result != (record.row_version, record.mapping_version):
                return
            request.app.state.quant_gateway.activate(service_id, record)
        else:
            await run_in_threadpool(request.app.state.freqtrade_client.activate, service_id, record)
        worker = getattr(request.app.state, "quant_worker", None)
        if worker is not None:
            await worker.refresh()
    except Exception:
        # The transaction has committed; a failed acknowledgement is not another save.
        return


@router.get("/bots/trading/commands/{service_id}/{engine}")
def read_commands(service_id: UUID, engine: Engine, request: Request):
    try:
        record = request.app.state.trading_replies.get(service_id, engine)
        return view(request, service_id, engine, record)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error


@router.put("/bots/trading/commands/{service_id}/{engine}/{action}")
async def save_command(
    service_id: UUID,
    engine: Engine,
    action: str,
    data: CommandWrite,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match", max_length=20)],
    operation: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
):
    version = if_match.strip('"')
    if not version.isascii() or not version.isdigit():
        raise HTTPException(400, "invalid_row_version")
    try:
        record = await run_in_threadpool(
            request.app.state.trading_replies.save,
            service_id,
            engine,
            action,
            data.keyword,
            data.templates,
            expected_version=int(version),
            operation_id=operation,
        )
    except ValueError as error:
        raise HTTPException(409 if "conflict" in str(error) else 422, str(error)) from error
    await activate(request, service_id, engine, record)
    return await run_in_threadpool(view, request, service_id, engine, record)


@router.post("/bots/trading/commands/{service_id}/{engine}/{action}/preview")
def preview_command(
    service_id: UUID, engine: Engine, action: str, data: PreviewWrite, request: Request
):
    try:
        record = request.app.state.trading_replies.get(service_id, engine)
        content = preview(
            engine,
            action,
            data.keyword,
            data.templates,
            data.variant,
            commands=record.commands,
            initial_cash=text(
                record.settings.futures_initial_cash
                if engine == "freqtrade" and action == "f_open"
                else record.settings.initial_cash
            ),
            settings=record.settings,
        )
        return {"content": content}
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
