"""Private control surface for the supervised QQ quant process."""

import asyncio
import hmac
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from uuid import UUID

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from al1s.adapters.postgres.quant_backtests import QuantBacktests
from al1s.adapters.postgres.quant_settings import QuantSettingsRecord
from al1s.quant.commands import QuantCommand
from al1s.quant.corporate_service import CorporateService
from al1s.quant.execution import PaperExecution
from al1s.quant.gateway import QuantGateway
from al1s.quant.health import QuantHealth
from al1s.quant.messages import QuantCommandRequest
from al1s.quant.retention import QuantRetention

DOMAIN_MESSAGES = {
    "quant_market_session_closed": "当前不在支持的交易时段，未受理模拟委托。",
    "quant_quote_stale": "行情已过期，未受理模拟委托。",
    "quant_quote_unavailable": "暂时无法取得有效行情，未受理模拟委托。",
    "quant_source_unavailable": "行情数据源暂不可用，请稍后重试。",
    "quant_source_empty": "数据源没有返回该证券或区间的行情。",
    "quant_source_units_unverified": "行情成交量单位无法核实，未执行请求。",
    "quant_market_data_missing": "历史行情不完整，无法执行该回测。",
    "quant_account_not_found": "尚未开户，请先发送模拟开户指令。",
    "quant_account_needs_review": "账户状态待核实，暂不能执行模拟委托。",
    "quant_order_pending": "账户有尚未完成的委托，请先查询模拟成交。",
    "quant_quantity_invalid": "股数不符合该板块的申报规则。",
    "quant_quantity_exceeds_available": "可卖股数不足，请检查 T+1 限制。",
    "quant_event_stale": "消息已过期，未受理模拟委托，请重新发送。",
    "quant_adjustment_unsupported": "当前数据源不支持所选复权方式，请调整配置。",
    "quant_backtest_invalid": "回测参数无效，请查看量化帮助。",
    "quant_backtest_range_invalid": "回测日期无效或包含尚未收盘的日期。",
    "quant_strategy_invalid": "仅支持买入持有和双均线策略。",
    "quant_code_invalid": "证券代码无效或市场不受支持。",
    "quant_code_mapping_unverified": "证券代码映射尚未核实，请使用当前代码。",
    "quant_corporate_data_unavailable": "分红送转数据暂不可用，未受理模拟委托。",
    "quant_corporate_needs_review": "分红送转权益待核实，暂不受理该证券的模拟委托。",
    "quant_source_busy": "行情服务繁忙，请稍后重试。",
    "quant_backtest_busy": "回测服务繁忙，请稍后重试。",
    "quant_frequency_unsupported": "当前数据源不支持所选周期或复权方式。",
}


def create_process_app(
    control_token: str,
    *,
    command_handler: Callable[[UUID, QuantCommand], str] | None = None,
    executor: PaperExecution | None = None,
    backtests: QuantBacktests | None = None,
    corporate: CorporateService | None = None,
    health: QuantHealth | None = None,
    retention: QuantRetention | None = None,
) -> FastAPI:
    if len(control_token.encode()) < 32:
        raise ValueError("quant_control_token_too_short")

    @asynccontextmanager
    async def lifespan(_app):
        stop = asyncio.Event()
        task = None
        corporate_task = None
        retention_task = None
        if executor is not None:
            await asyncio.to_thread(executor.orders.recover_interrupted)
            if backtests is not None:
                await asyncio.to_thread(backtests.recover_interrupted)

            async def poll():
                while not stop.is_set():
                    try:
                        await asyncio.to_thread(executor.run_once)
                    except Exception as error:
                        structlog.get_logger().warning(
                            "quant_execution_poll_failed", error=type(error).__name__
                        )
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=15)

            task = asyncio.create_task(poll())
        if corporate is not None:

            async def sync_corporate():
                while not stop.is_set():
                    try:
                        await asyncio.to_thread(corporate.run_once)
                    except Exception as error:
                        structlog.get_logger().warning(
                            "quant_corporate_poll_failed", error=type(error).__name__
                        )
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=3600)

            corporate_task = asyncio.create_task(sync_corporate())
        if retention is not None:

            async def cleanup_cache():
                while not stop.is_set():
                    try:
                        await asyncio.to_thread(retention.run_once)
                    except Exception as error:
                        structlog.get_logger().warning(
                            "quant_cache_cleanup_failed", error=type(error).__name__
                        )
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=86400)

            retention_task = asyncio.create_task(cleanup_cache())
        try:
            yield
        finally:
            stop.set()
            if executor is not None:
                executor.stop()
            if corporate is not None:
                corporate.stop()
            if retention is not None:
                retention.stop()
            if task:
                await asyncio.gather(task, return_exceptions=True)
            if corporate_task:
                await asyncio.gather(corporate_task, return_exceptions=True)
            if retention_task:
                await asyncio.gather(retention_task, return_exceptions=True)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    gateway = QuantGateway()

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        supplied = request.headers.get("x-quant-control", "")
        if not hmac.compare_digest(supplied.encode(), control_token.encode()):
            return JSONResponse({"detail": "quant_control_required"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/internal/config/{service_id}")
    def read_config(service_id: UUID) -> dict[str, int]:
        row, mapping = gateway.applied_versions(service_id)
        return {"row_version": row, "mapping_version": mapping}

    @app.get("/internal/health")
    def status() -> dict:
        if health is None:
            raise HTTPException(503, "quant_status_unavailable")
        return health.read()

    @app.put("/internal/config/{service_id}")
    def activate(service_id: UUID, record: QuantSettingsRecord) -> dict[str, int]:
        row, mapping = gateway.activate(service_id, record)
        return {"row_version": row, "mapping_version": mapping}

    @app.post("/internal/command/{service_id}")
    def execute(service_id: UUID, data: QuantCommandRequest) -> dict[str, str]:
        row, mapping = gateway.applied_versions(service_id)
        if command_handler is None or row < data.settings_version or mapping < data.mapping_version:
            raise HTTPException(503, "quant_command_unavailable")
        try:
            content = command_handler(service_id, data.command())
        except ValueError as error:
            content = DOMAIN_MESSAGES.get(str(error), "量化请求未完成，请检查输入或稍后查询。")
        if not isinstance(content, str) or not 1 <= len(content) <= 4096:
            raise HTTPException(503, "quant_command_result_invalid")
        return {"content": content}

    return app
