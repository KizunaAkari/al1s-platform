"""QQ-facing account and built-in strategy command handling."""

from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from al1s.adapters.postgres.quant_accounts import QuantAccounts
from al1s.adapters.postgres.quant_orders import QuantOrders
from al1s.quant.commands import QuantCommand
from al1s.quant.market_rules import CHINA
from al1s.quant.presentation import ORDER_STATUS
from al1s.quant.provider_routing import PROVIDER_NAMES
from al1s.trading_replies.help_text import quant_help
from al1s.trading_replies.render import list_reply, render, reply

OrderHandler = Callable[[UUID, QuantCommand], str]


class QuantService:
    def __init__(
        self,
        accounts: QuantAccounts,
        orders: QuantOrders,
        *,
        trade: OrderHandler | None = None,
        backtest: OrderHandler | None = None,
        name_source=None,
        instruments=None,
    ):
        self.accounts, self.orders, self.trade, self.backtest = accounts, orders, trade, backtest
        self.name_source, self.instruments = name_source, instruments

    def _account_names(self, rows) -> dict[tuple[str, str], str]:
        if self.name_source is None or self.instruments is None:
            return {}
        missing = []
        for row in rows:
            code = getattr(row, "current_code", None) or getattr(row, "code", None)
            market, name = getattr(row, "market", None), getattr(row, "name", None)
            if (isinstance(code, str) and isinstance(market, str)
                    and (not isinstance(name, str) or not name.strip() or name.strip() == code)):
                missing.append((market, code))
        if not missing:
            return {}
        names = self.name_source.security_names(list(dict.fromkeys(missing)))
        if names:
            with suppress(SQLAlchemyError, ValueError):
                self.instruments.cache_names(names)
        return names

    @staticmethod
    def _account_name(row, code: str, names: dict[tuple[str, str], str]) -> str | None:
        name = getattr(row, "name", None)
        if isinstance(name, str) and name.strip() and name.strip() != code:
            return name.strip()
        return names.get((getattr(row, "market", None), code))

    def _select_summary(self, user, settings):
        chosen = None
        for provider in settings.provider_order:
            candidate = self.accounts.summary(user, provider=provider)
            if candidate is None:
                return None
            if chosen is None or (
                chosen[1].market_value is None and candidate.priced > chosen[1].priced
            ):
                chosen = (provider, candidate)
            if candidate.market_value is not None:
                if chosen[1].market_value is None:
                    chosen = (provider, candidate)
                oldest = candidate.oldest_quote
                if (oldest is None or 0 <= (datetime.now(UTC) - (
                    oldest if oldest.tzinfo else oldest.replace(tzinfo=UTC)
                ).astimezone(UTC)).total_seconds() <= settings.max_quote_age_seconds):
                    chosen = (provider, candidate)
                    break
        return chosen

    def query_for_ai(self, source, settings, kind: str, page: int) -> dict:
        if kind not in {"balance", "positions", "trades"} or not 1 <= page <= 1000:
            raise ValueError("quant_read_invalid")
        user = source.sender_id
        if kind == "balance":
            selected = self._select_summary(user, settings)
            if selected is None:
                return {"status": "failed", "code": "account_missing"}
            provider, summary = selected
            oldest = summary.oldest_quote
            if oldest is not None and oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - oldest.astimezone(UTC)).total_seconds() if oldest else None
            data = {"unit": "CNY", "cash": str(summary.account.cash),
                    "market_value": str(summary.market_value) if summary.market_value is not None else None,
                    "total_assets": str(summary.account.cash + summary.market_value)
                        if summary.market_value is not None else None,
                    "position_count": summary.positions, "priced_count": summary.priced,
                    "quote_provider": provider, "oldest_quote": oldest.isoformat() if oldest else None,
                    "quote_stale": age is not None and not 0 <= age <= settings.max_quote_age_seconds,
                    "account_state": summary.account.state}
        else:
            if self.accounts.get(user) is None:
                return {"status": "failed", "code": "account_missing"}
            if kind == "positions":
                rows = self.accounts.positions(user, limit=25, offset=(page - 1) * 25)
                names = self._account_names(rows) if source.scope_kind == "admin" else {}
                data = {"page": page, "positions": [dict(code=row.current_code,
                    name=(self._account_name(row, row.current_code, names)
                          if source.scope_kind == "admin" else row.name),
                    market=row.market, quantity=row.quantity, available=row.available,
                    total_cost=str(row.total_cost)) for row in rows]}
            else:
                rows = self.orders.history(user, limit=25, offset=(page - 1) * 25)
                names = self._account_names(rows) if source.scope_kind == "admin" else {}
                data = {"page": page, "trades": [dict(id=str(row.id), code=row.code,
                    **({"name": self._account_name(row, row.code, names)}
                       if source.scope_kind == "admin" else {}),
                    side=row.side, quantity=row.quantity, status=row.status,
                    filled_quantity=row.filled_quantity, total_fees=str(row.total_fees),
                    quote_provider=row.quote_provider, execution_provider=row.execution_provider,
                    created_at=row.created_at.isoformat()) for row in rows]}
        return {"status": "success", "source": "akquant", "observed_at": datetime.now(UTC).isoformat(),
                "data": data}

    def execute(self, service_id: UUID, command: QuantCommand) -> str:
        if command.settings is None:
            raise ValueError("quant_snapshot_missing")
        user, args = command.source.sender_id, command.arguments
        if command.action == "help":
            if args == ("费率",):
                return reply(command, "fees", dict(
                    commission_percent=command.settings.commission_rate * 100,
                    minimum_commission=command.settings.minimum_commission,
                    tax_percent=command.settings.stamp_tax_rate * 100,
                    slippage_percent=command.settings.slippage * 100))
            if args:
                return "帮助指令仅支持费率参数。"
            return reply(command, "main", {"help_text": quant_help(command)})
        if command.action == "open":
            if args:
                return "开户指令不接受参数，账户只属于发送者。"
            account, created = self.accounts.open(user, command.settings.initial_cash)
            return reply(command, "main" if created else "existing",
                         dict(cash=account.cash, status="已创建" if created else "已存在"))
        if command.action == "balance":
            if args:
                return "资产指令不接受参数。"
            chosen = None
            for provider in command.settings.provider_order:
                candidate = self.accounts.summary(user, provider=provider)
                if candidate is None:
                    return "尚未开户，请先发送模拟开户指令。"
                if chosen is None or (
                    chosen[1].market_value is None and candidate.priced > chosen[1].priced
                ):
                    chosen = (provider, candidate)
                if candidate.market_value is not None:
                    if chosen[1].market_value is None:
                        chosen = (provider, candidate)
                    oldest = candidate.oldest_quote
                    if (
                        oldest is None
                        or 0
                        <= (
                            datetime.now(UTC)
                            - (oldest if oldest.tzinfo else oldest.replace(tzinfo=UTC)).astimezone(
                                UTC
                            )
                        ).total_seconds()
                        <= command.settings.max_quote_age_seconds
                    ):
                        chosen = (provider, candidate)
                        break
            assert chosen is not None
            provider, summary = chosen
            warning = "账户状态待核实。" if summary.account.state != "active" else ""
            values = dict(cash=summary.account.cash, position_count=summary.positions,
                          priced_count=summary.priced, account_warning=warning)
            if summary.market_value is None:
                result = reply(command, "incomplete", values)
            else:
                quote_info = ""
                if summary.oldest_quote:
                    oldest = summary.oldest_quote
                    if oldest.tzinfo is None:
                        oldest = oldest.replace(tzinfo=UTC)
                    age = (datetime.now(UTC) - oldest.astimezone(UTC)).total_seconds()
                    quote_info = (f"最早行情 {oldest.astimezone(CHINA):%Y-%m-%d %H:%M:%S}，"
                                  f"来源 {PROVIDER_NAMES[provider]}"
                                  + ("（已过期）" if age < 0 or age > command.settings.max_quote_age_seconds else "") + "。")
                values.update(market_value=summary.market_value,
                    total_assets=summary.account.cash + summary.market_value, quote_info=quote_info)
                result = reply(command, "main", values)
            result += warning if warning and warning not in result else ""
            return result if len(result) <= 4096 else render("quant", "balance",
                "incomplete" if summary.market_value is None else "main", values)
        if command.action in {"positions", "orders"}:
            if len(args) > 1 or (args and (not args[0].isdigit() or not 1 <= int(args[0]) <= 1000)):
                return "请输入有效页码（1–1000）。"
            page = int(args[0]) if args else 1
            if self.accounts.get(user) is None:
                return "尚未开户，请先发送模拟开户指令。"
            if command.action == "positions":
                rows = self.accounts.positions(user, limit=25, offset=(page - 1) * 25)
                if not rows:
                    return reply(command, "empty", {"page": page})
                values = [dict(code=row.current_code, name=row.name,
                    quantity=row.quantity, available=row.available, total_cost=row.total_cost) for row in rows]
                return list_reply(command, values, dict(page=page))
            rows = self.orders.history(user, limit=25, offset=(page - 1) * 25)
            if not rows:
                return reply(command, "empty", {"page": page})
            values = [dict(order_id=str(row.id)[:8], code=row.code,
                side="买入" if row.side == "buy" else "卖出", quantity=row.quantity,
                status=ORDER_STATUS.get(row.status, "待核实"), filled_quantity=row.filled_quantity,
                total_fees=row.total_fees, quote_provider=PROVIDER_NAMES.get(row.quote_provider, row.quote_provider),
                execution_provider=PROVIDER_NAMES.get(row.execution_provider, row.execution_provider)
                    if row.execution_provider else "尚未撮合") for row in rows]
            return list_reply(command, values, dict(page=page))
        if command.action in {"buy", "sell"} and self.trade:
            return self.trade(service_id, command)
        if command.action == "backtest" and self.backtest:
            return self.backtest(service_id, command)
        return "量化服务尚未准备好，请稍后重试。"
