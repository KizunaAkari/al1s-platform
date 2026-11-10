from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from uuid import UUID

from al1s.freqtrade.capital_service import (
    CAPITAL_ACTIONS,
    execute_capital,
    parse_capital,
    receipt_matches,
)
from al1s.freqtrade.commands import parse_order
from al1s.freqtrade.contracts import WRITE_ACTIONS, FreqtradeCommand, FreqtradeReadRequest
from al1s.freqtrade.futures_service import (
    execute_futures,
)
from al1s.freqtrade.manager import confirm_exit
from al1s.freqtrade.presentation import history_values, number, position_values
from al1s.trading_replies.help_text import freqtrade_help
from al1s.trading_replies.render import list_reply, reply, text


def authorized_ai_source(settings, source, market: str) -> bool:
    return bool(settings.enabled and source.guild_id is None
        and not (source.scope_kind == "qq_group" and source.target_id not in settings.groups)
        and not (source.scope_kind == "qq_private" and (
            not settings.private_enabled or source.target_id != source.sender_id))
        and source.scope_kind in {"qq_group", "qq_private"}
        and not (market == "futures" and not settings.futures_enabled))


class FreqtradeService:
    def __init__(self, repository, manager):
        self.repository, self.manager = repository, manager
        self._guard = Lock()
        self._locks: dict[str, Lock] = {}

    def query_for_ai(self, service_id: UUID, request: FreqtradeReadRequest, *, admin: bool = False) -> dict:
        if admin:
            with self._guard:
                lock = self._locks.setdefault(request.source.sender_id, Lock())
            with lock:
                return self._query(service_id, request, admin=True)
        return self._query(service_id, request, admin=False)

    def _query(self, service_id: UUID, request: FreqtradeReadRequest, *, admin: bool) -> dict:
        settings = self.repository.get(service_id).settings
        source = request.source
        authorized = (source.scope_kind == "admin" and settings.enabled
                      and (request.market != "futures" or settings.futures_enabled)) if admin else authorized_ai_source(settings, source, request.market)
        if not authorized:
            return {"status": "denied", "code": "trading_scope_denied"}
        profile = self.repository.profile(source.sender_id, market=request.market)
        if profile is None:
            return {"status": "failed", "code": "account_missing"}
        if admin:
            if not self.manager.has_namespace(profile):
                return {"status": "failed", "code": "account_data_missing"}
            try:
                native = self.manager.ensure(profile, self.repository.get(profile.service_id).settings)
            except (OSError, ValueError):
                return {"status": "busy", "code": "account_engine_unavailable"}
        else:
            with self.manager.lock:
                native = self.manager.instances.get(profile.id)
                if (profile.id in getattr(self.manager, '_busy', set()) or native is None
                        or native.process.poll() is not None):
                    return {"status": "busy", "code": "account_engine_inactive"}
        futures = request.market == "futures"
        page_size = 10 if futures else 20
        if request.kind == "balance":
            raw = native.call("balance")
            currencies = [dict(currency=row.get("currency"), free=number(row.get("free")),
                               balance=number(row.get("balance")), used=number(row.get("used")))
                          for row in raw.get("currencies", [])
                          if row.get("balance") or ((futures or admin) and row.get("currency") == "USDT")][:20]
            data = {"currencies": currencies}
            if futures:
                profits = [number(row.get("profit_abs")) for row in native.call("status")]
                data["unrealized_pnl"] = (sum(profits, Decimal(0))
                                          if all(value is not None for value in profits) else None)
            if getattr(native, "capital_version", 0) == 1:
                capital = native.call("al1s/capital")
                data["deposits_total"] = number(capital["deposits_total"])
                data["added_margin"] = sum(
                    (Decimal(item["added_margin"]) for item in capital["margins"]), Decimal(0)
                )
        elif request.kind == "positions":
            start = (request.page - 1) * page_size
            rows = sorted(native.call("status"), key=lambda row: row["trade_id"])[start:start + page_size]
            if futures and rows and getattr(native, "capital_version", 0) == 1:
                extras = {item["trade_id"]: item.get("added_margin", "0")
                          for item in native.call("al1s/capital")["margins"]}
                rows = [{**row, "added_margin": extras.get(row["trade_id"], "0")} for row in rows]
            data = {"page": request.page, "positions": [position_values(row) for row in rows]}
        else:
            active = native.call("status")
            start = (request.page - 1) * page_size
            active_page = active[start:start + page_size]
            limit = page_size - len(active_page)
            offset = max(0, start - len(active))
            history = native.call(f"trades?limit={limit}&offset={offset}").get("trades", []) if limit else []
            merged = {row["trade_id"]: row for row in [*active_page, *history]}
            rows = list(merged.values())[:page_size]
            recovery = self.reconcile(profile, rows.copy(), native) if admin and profile.state == "unknown" else ""
            if recovery:
                profile = self.repository.profile(source.sender_id, market=request.market)
            data = {"page": request.page, "trades": [
                {**history_values(row), "state": self.trade_state(row), "last_order": self.last_order(row),
                 "amount": number(row.get("amount")), "opened_at": row.get("open_date"), "closed_at": row.get("close_date")}
                if admin else history_values(row) for row in rows]}
            if recovery:
                data["recovery_message"] = recovery.strip()

        def serialize(value):
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, list):
                return [serialize(item) for item in value]
            if isinstance(value, dict):
                return {key: serialize(item) for key, item in value.items()}
            return value

        return {"status": "success", "source": "freqtrade", "market": request.market,
                "observed_at": datetime.now(UTC).isoformat(), "account_state": profile.state,
                "data": serialize(data)}

    def execute(self, service_id: UUID, command: FreqtradeCommand) -> str:
        orders_word = command.commands.get(
            "f_orders" if command.market == "futures" else "orders", "成交记录"
        )
        with self._guard:
            lock = self._locks.setdefault(command.source.sender_id, Lock())
        with lock:
            receipt, created = self.repository.claim(service_id, command)
            if not created:
                return (
                    receipt.result
                    or f"请求正在处理或结果待核实，请稍后查询{orders_word}，勿重复下单。"
                )
            try:
                if not 0 <= datetime.now(UTC).timestamp() - command.event_timestamp <= 120:
                    raise ValueError("消息已过期，未执行请求，请重新发送。")
                result = self._execute(service_id, command, receipt.id)
            except ValueError as error:
                result = str(error)
            except Exception:
                unknown = command.action in WRITE_ACTIONS
                profile = (
                    self.repository.profile(command.source.sender_id, market="futures")
                    if command.market == "futures"
                    else self.repository.profile(command.source.sender_id)
                )
                if unknown and profile is not None:
                    if command.market == "futures":
                        self.repository.set_profile(profile.id, market="futures", state="unknown")
                    else:
                        self.repository.set_profile(profile.id, state="unknown")
                result = (
                    f"请求结果待核实，请查询{orders_word}，勿重复下单。"
                    if unknown
                    else "Freqtrade暂不可用，账户数据已保留，请稍后重试。"
                )
                self.repository.complete(receipt.id, result, unknown=unknown)
                return result
            self.repository.complete(receipt.id, result)
            return result

    def _execute(self, service_id: UUID, command: FreqtradeCommand, request_id: UUID) -> str:
        args, user = command.arguments, command.source.sender_id
        if command.action == "help":
            if args:
                raise ValueError("帮助指令不接受参数。")
            return reply(command, "main", {"help_text": self.help(command)}, engine="freqtrade")
        if command.market == "futures":
            return execute_futures(self, service_id, command, request_id)
        profile = self.repository.profile(user)
        if command.action == "open":
            if args:
                raise ValueError("开户指令不接受参数。")
            profile, created = self.repository.open(user, service_id, command.settings)
            self.manager.ensure(profile, command.settings)
            return reply(command, "main" if created else "existing",
                         dict(initial_cash=profile.initial_cash, status="已创建" if created else "已存在"), engine="freqtrade")
        if profile is None:
            return f"尚未开户，请先发送 {command.commands['open']}。"
        if command.action == "deposit":
            parse_capital(command.action, args)
        settings = self.repository.get(profile.service_id).settings
        native = self.manager.ensure(profile, settings)
        if command.action == "deposit":
            if profile.state != "ready" or self.repository.blocking_request(
                user, request_id, market="spot"
            ):
                return f"账户有待核实操作，请查询 {command.commands['orders']}。"
            return execute_capital(self, native, command, request_id)
        if command.action in {"buy", "sell"}:
            if profile.state != "ready":
                return "账户有结果待核实的请求，暂不能执行买卖；请查询币成交并联系管理员核对。"
            return self.trade(native, command, settings.fee, request_id)
        return self.query(native, command, profile)

    def trade(self, native, command: FreqtradeCommand, fee: Decimal, request_id: UUID) -> str:
        pair, amount = parse_order(command.action, command.arguments)
        try:
            trades = native.call("status")
        except (OSError, ValueError) as error:
            raise ValueError("无法核验持仓，本次未发送买卖委托，请稍后重试。") from error
        current = next((trade for trade in trades if trade["pair"] == pair), None)
        if current is not None and current.get("has_open_orders"):
            raise ValueError("该币对有尚未完成的委托，请先查询币成交。")
        self.repository.prepare_native(request_id, current)
        if command.action == "buy":
            result = native.call(
                "forceenter",
                {
                    "pair": pair,
                    "side": "long",
                    "stakeamount": float(amount),
                    "ordertype": "market",
                    "entry_tag": "al1s-" + request_id.hex,
                },
            )
            trade_id = result.get("trade_id")
            if not isinstance(trade_id, int):
                if str(result.get("status", "")).startswith("Error"):
                    raise ValueError("框架未受理该买入委托，请检查可用资金和最小交易金额。")
                raise OSError("freqtrade_order_result_unconfirmed")
        else:
            if current is None:
                raise ValueError("没有该币对的可卖持仓。")
            if amount is not None and amount > Decimal(str(current["amount"])):
                raise ValueError("卖出币量超过当前持仓。")
            data = {"tradeid": str(current["trade_id"]), "ordertype": "market"}
            if amount is not None:
                data["amount"] = float(amount)
            confirm_exit(native.call("forceexit", data), current["trade_id"])
            trade_id = current["trade_id"]
        self.manager.market_success = datetime.now(UTC).isoformat()
        return reply(command, "main", dict(pair=pair, side="买入" if command.action == "buy" else "卖出",
            trade_id=trade_id, amount_text=(text(amount)+" USDT" if command.action == "buy" else
            (text(amount)+"币" if amount is not None else "全部")), orders_command=command.commands["orders"],
            fee_percent=fee*100), engine="freqtrade")

    def query(self, native, command: FreqtradeCommand, profile) -> str:
        args = command.arguments
        action = command.action.removeprefix("f_")
        futures = command.market == "futures"
        if action == "balance":
            if args:
                raise ValueError("资产查询不接受参数。")
            result = native.call("balance")
            currencies = [row for row in result.get("currencies", [])
                          if row.get("balance") or (futures and row.get("currency") == "USDT")]
            currency_values = [dict(currency=row["currency"], free=number(row.get("free")),
                balance=number(row.get("balance")), used=number(row.get("used"))) for row in currencies[:20]]
            pnl = None
            if futures:
                profits = [row.get("profit_abs") for row in native.call("status")]
                if all(value is not None for value in profits):
                    pnl = sum((Decimal(str(value)) for value in profits), Decimal(0))
            capital_summary, deposits, extra = "", None, None
            if getattr(native, "capital_version", 0) == 1:
                capital = native.call("al1s/capital")
                deposits = number(capital["deposits_total"])
                extra = sum((Decimal(item["added_margin"]) for item in capital["margins"]), Decimal(0))
                capital_summary = (f"\n累计虚拟充值 {text(deposits)} USDT；追加保证金占用 {text(extra)} USDT。")
            return list_reply(command, currency_values, dict(rows="暂无资产。", unrealized_pnl=pnl,
                capital_summary=capital_summary, deposits_total=deposits, added_margin=extra), engine="freqtrade")
        if len(args) > 1 or (args and (not args[0].isdigit() or not 1 <= int(args[0]) <= 1000)):
            raise ValueError("页码范围为1–1000，不填时默认第1页。")
        page = int(args[0]) if args else 1
        page_size = 10 if futures else 20
        if action == "positions":
            rows = sorted(native.call("status"), key=lambda row: row["trade_id"])[
                (page - 1) * page_size : page * page_size
            ]
            if futures and rows and getattr(native, "capital_version", 0) == 1:
                extra_by_id = {
                    row["trade_id"]: row for row in native.call("al1s/capital")["margins"]
                }
                rows = [
                    {
                        **row,
                        "added_margin": extra_by_id.get(row["trade_id"], {}).get(
                            "added_margin", "0"
                        ),
                    }
                    for row in rows
                ]
            if not rows:
                return reply(command, "empty", {"page": page}, engine="freqtrade")
            values = [position_values(row) for row in rows]
            warning = ("部分持仓未能取得原生浮动盈亏，请检查行情后重试。\n"
                       if futures and any(row["profit_abs"] is None for row in values) else "")
            return list_reply(command, values, dict(page=page), engine="freqtrade", prefix=warning)
        active = native.call("status")
        start = (page - 1) * page_size
        active_page = active[start : start + page_size]
        offset = max(0, start - len(active))
        limit = page_size - len(active_page)
        history = (
            native.call(f"trades?limit={limit}&offset={offset}").get("trades", []) if limit else []
        )
        merged = {row["trade_id"]: row for row in [*active_page, *history]}
        recovery = (
            self.reconcile(profile, list(merged.values()), native)
            if profile.state == "unknown" or futures
            else ""
        )
        rows = list(merged.values())[:page_size]
        if not rows:
            return list_reply(command, [], {"page": page}, engine="freqtrade", prefix=recovery)
        values = [{**history_values(row), "state": self.trade_state(row), "last_order": self.last_order(row)} for row in rows]
        return list_reply(command, values, dict(page=page), engine="freqtrade", prefix=recovery)

    @staticmethod
    def trade_state(trade: dict) -> str:
        if trade.get("has_open_orders"):
            return "委托处理中"
        return "持仓中" if trade.get("is_open") else "已平仓"

    @staticmethod
    def last_order(trade: dict) -> str:
        orders = trade.get("orders", [])
        if not orders:
            return "暂无委托明细"
        order = orders[-1]
        return f"最近委托 {order.get('status', '待核实')}，成交 {order.get('filled', '待查询')} 币"

    def reconcile(self, profile, trades: list[dict], native=None) -> str:
        market = getattr(profile, "market", "spot")
        pending = self.repository.unresolved(profile.qq_id, market=market)
        if not pending:
            return ""
        if native is not None:
            for receipt in pending:
                if receipt.action in CAPITAL_ACTIONS:
                    continue
                if receipt.native_trade_id is not None and not any(
                    trade["trade_id"] == receipt.native_trade_id for trade in trades
                ):
                    with suppress(OSError, ValueError):
                        trades.append(native.call(f"trade/{receipt.native_trade_id}"))
        recovered = []
        for receipt in pending:
            if receipt.action in CAPITAL_ACTIONS:
                if native is not None:
                    with suppress(OSError, ValueError):
                        evidence = native.call(f"al1s/capital/operations/{receipt.id}")
                        if receipt_matches(receipt, evidence):
                            recovered.append((receipt.id, receipt.native_trade_id))
                continue
            found = next(
                (
                    trade
                    for trade in trades
                    if (
                        receipt.native_trade_id == trade["trade_id"]
                        and len(trade.get("orders", [])) > (receipt.native_order_count or 0)
                    )
                    or (
                        receipt.action in {"buy", "long", "short"}
                        and trade.get("enter_tag") == "al1s-" + receipt.id.hex
                    )
                ),
                None,
            )
            if found is not None:
                recovered.append((receipt.id, found["trade_id"]))
        self.repository.finish_recovery(
            profile.id,
            [identity for identity, _ in recovered],
            all_recovered=bool(pending and len(recovered) == len(pending)),
            market=market,
        )
        if pending and len(recovered) == len(pending):
            return "已核对框架操作记录，账户恢复可操作；未重复执行。\n"
        return "账户仍有结果待核实的请求，继续保留写入保护。\n"

    @staticmethod
    def help(command: FreqtradeCommand) -> str:
        return freqtrade_help(command)
