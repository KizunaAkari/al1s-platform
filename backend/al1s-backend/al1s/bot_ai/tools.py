import asyncio
import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from al1s.bot_ai import market
from al1s.bot_ai.contracts import AiEnvelope
from al1s.freqtrade.contracts import FreqtradeChartRequest, FreqtradeReadRequest
from al1s.meme_workshop.images import decode_image
from al1s.quant.messages import QuantReadRequest


class QueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: Literal["ashare", "spot", "futures"]
    kind: Literal["balance", "positions", "trades"]
    page: int = Field(default=1, ge=1, le=1000, strict=True)


class ChartArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: Literal["BTC", "ETH", "SOL"]
    market: Literal["spot", "futures"]
    period: Literal["15分钟", "1小时", "4小时", "1天", "7天"] = "1小时"


class MarketArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: Literal["ashare", "spot", "futures", "gold", "forex"]
    symbol: str = Field(min_length=1, max_length=20)


class FeedArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=64)


class MemeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_key: str = Field(min_length=1, max_length=32)
    target: str = Field(default="self", min_length=1, max_length=20)


class BotAiTools:
    def __init__(self, repository, hub, quant_client, freqtrade_client, media_root: Path,
                 meme_sender) -> None:
        self.repository, self.hub = repository, hub
        self.quant, self.freqtrade = quant_client, freqtrade_client
        self.media_root, self.meme_sender = Path(media_root).resolve(), meme_sender
        self._slots = asyncio.Semaphore(4)
        self._meme_tasks: dict[object, asyncio.Task] = {}

    async def _allowed(self, item: AiEnvelope) -> bool:
        allowed = bool(self.hub.is_active(item.request_id)
            and datetime.now(UTC) < item.deadline
            and await asyncio.to_thread(self.repository.allowed, item.service_id,
                                        item.source, item.config_version))
        return allowed and self.hub.is_active(item.request_id)

    def _run_active(self, item: AiEnvelope, operation, *args):
        if not self.hub.is_active(item.request_id) or datetime.now(UTC) >= item.deadline:
            return {"status": "denied", "code": "ai_scope_revoked"}
        return operation(*args)

    async def execute(self, item: AiEnvelope, call_id: str, name: str, arguments: dict) -> dict:
        try:
            if not await self._allowed(item):
                return {"status": "denied", "code": "ai_scope_revoked"}
            if self._slots.locked():
                return {"status": "busy", "code": "ai_tool_busy"}
            async with self._slots:
                if name == "query_trading":
                    args = QueryArgs.model_validate(arguments)
                    if args.market == "ashare":
                        if self.quant is None:
                            return {"status": "busy", "code": "quant_unavailable"}
                        request = QuantReadRequest(source=item.source, kind=args.kind, page=args.page)
                        result = await asyncio.wait_for(asyncio.to_thread(
                            self._run_active, item, self.quant.query_for_ai, item.service_id, request), timeout=10)
                    else:
                        if self.freqtrade is None:
                            return {"status": "busy", "code": "freqtrade_unavailable"}
                        request = FreqtradeReadRequest(source=item.source, market=args.market,
                                                       kind=args.kind, page=args.page)
                        result = await asyncio.wait_for(asyncio.to_thread(
                            self._run_active, item, self.freqtrade.query_for_ai, item.service_id, request), timeout=10)
                elif name == "market_quote":
                    args = MarketArgs.model_validate(arguments)
                    if args.market in {"spot", "futures"}:
                        symbol = args.symbol.upper().removesuffix(":USDT").removesuffix("/USDT")
                        chart = ChartArgs(symbol=symbol, market=args.market, period="15分钟")
                        if self.freqtrade is None:
                            return {"status": "busy", "code": "freqtrade_unavailable"}
                        request = FreqtradeChartRequest(source=item.source, market=chart.market,
                                                        symbol=chart.symbol, period=chart.period)
                        result = await asyncio.wait_for(asyncio.to_thread(
                            self._run_active, item, self.freqtrade.chart_for_ai, item.service_id, request), timeout=10)
                        if result.get("status") == "success":
                            result = {"status": "success", "source": result["source"],
                                "symbol": result["symbol"] + "/USDT" + (":USDT" if args.market == "futures" else ""),
                                "market": args.market,
                                "unit": "USDT", "price": result["last_close"],
                                "price_type": "1分钟完整K线收盘价", "quote_time": result["last_bar_time"],
                                "collected_at": result["collected_at"]}
                    else:
                        result = await asyncio.wait_for(asyncio.to_thread(
                            self._run_active, item, market.public_quote, args.market, args.symbol), timeout=8)
                elif name == "search_feeds":
                    args = FeedArgs.model_validate(arguments)
                    result = await asyncio.wait_for(asyncio.to_thread(
                        self._run_active, item, market.search_feeds, args.query), timeout=8)
                elif name == "crypto_chart":
                    args = ChartArgs.model_validate(arguments)
                    if self.freqtrade is None:
                        return {"status": "busy", "code": "freqtrade_unavailable"}
                    request = FreqtradeChartRequest(source=item.source, market=args.market,
                                                    symbol=args.symbol, period=args.period)
                    result = await asyncio.wait_for(asyncio.to_thread(
                        self._run_active, item, self.freqtrade.chart_for_ai, item.service_id, request), timeout=10)
                    if result.get("status") == "success":
                        if not await self._allowed(item):
                            return {"status": "denied", "code": "ai_scope_revoked"}
                        result = await asyncio.to_thread(self._run_active, item,
                                                         self._save_chart, item, call_id, result)
                elif name == "send_meme":
                    args = MemeArgs.model_validate(arguments)
                    if args.target != "self" and (item.source.scope_kind != "qq_group"
                                                   or args.target not in item.mentions):
                        return {"status": "denied", "code": "ai_meme_target_denied"}
                    task = self._meme_tasks.get(item.request_id)
                    if task is None:
                        task = asyncio.create_task(self.meme_sender(item, args.template_key, args.target))
                        self._meme_tasks[item.request_id] = task
                    try:
                        result = await asyncio.wait_for(asyncio.shield(task), timeout=20)
                    except TimeoutError:
                        return {"status": "unknown", "code": "ai_meme_unconfirmed"}
                else:
                    return {"status": "denied", "code": "ai_tool_not_allowed"}
                if not await self._allowed(item):
                    return {"status": "denied", "code": "ai_scope_revoked"}
                if not isinstance(result, dict) or len(json.dumps(result, ensure_ascii=False)) > 65536:
                    return {"status": "failed", "code": "ai_tool_result_invalid"}
                return result
        except ValidationError:
            return {"status": "denied", "code": "ai_tool_arguments_invalid"}
        except TimeoutError:
            return {"status": "busy", "code": "ai_tool_timeout"}
        except (OSError, ValueError, TypeError):
            return {"status": "failed", "code": "ai_tool_unavailable"}

    def _save_chart(self, item: AiEnvelope, call_id: str, result: dict) -> dict:
        body = base64.b64decode(result.get("image", ""), validate=True)
        if len(body) > 524288 or not body.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("ai_chart_invalid")
        decode_image(body)
        directory = (self.media_root / str(item.request_id)).resolve()
        if directory.parent != self.media_root or not directory.is_dir():
            raise ValueError("ai_chart_directory_invalid")
        name = hashlib.sha256(call_id.encode()).hexdigest()[:16]
        path = directory / f"chart-{name}.png"
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
        with os.fdopen(descriptor, "wb") as file:
            file.write(body)
        return {**{key: value for key, value in result.items() if key != "image"},
                "media_ref": str(path)}

    def forget(self, request_id) -> None:
        task = self._meme_tasks.get(request_id)
        if task is not None:
            if task.done():
                self._meme_tasks.pop(request_id, None)
            else:
                task.add_done_callback(lambda _task: self._meme_tasks.pop(request_id, None))

    def pending_meme(self, request_id):
        task = self._meme_tasks.get(request_id)
        return task if task is not None and not task.done() else None
