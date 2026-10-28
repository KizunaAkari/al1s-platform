"""QQ-facing account and built-in strategy command handling."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from al1s.adapters.postgres.quant_accounts import QuantAccounts
from al1s.adapters.postgres.quant_orders import QuantOrders
from al1s.quant.commands import QuantCommand
from al1s.quant.market_rules import CHINA
from al1s.quant.provider_routing import PROVIDER_NAMES

OrderHandler = Callable[[UUID, QuantCommand], str]
ORDER_STATUS = {
    "accepted": "已受理",
    "processing": "处理中",
    "partially_filled": "部分成交",
    "filled": "已成交",
    "rejected": "已拒绝",
    "expired": "已过期",
    "unknown": "待核实",
}


class QuantService:
    def __init__(
        self,
        accounts: QuantAccounts,
        orders: QuantOrders,
        *,
        trade: OrderHandler | None = None,
        backtest: OrderHandler | None = None,
    ):
        self.accounts, self.orders, self.trade, self.backtest = accounts, orders, trade, backtest

    def execute(self, service_id: UUID, command: QuantCommand) -> str:
        if command.settings is None:
            raise ValueError("quant_snapshot_missing")
        user, args = command.source.sender_id, command.arguments
        if command.action == "help":
            if args:
                return "帮助指令不接受参数。"
            words = dict(command.command_words)
            return (
                "模拟交易指令：\n" + "\n".join(words.values()) + "\n买卖：关键词 股票代码 股数；"
                "回测：关键词 买入持有或双均线 股票代码 起始日期 结束日期"
                " [日线或1分钟] [不复权或前复权或后复权] [快线 慢线]。"
                "持仓和成交可附页码。仅为模拟，不连接真实账户。"
            )
        if command.action == "open":
            if args:
                return "开户指令不接受参数，账户只属于发送者。"
            account, created = self.accounts.open(user, command.settings.initial_cash)
            return (
                "模拟账户已创建" if created else "模拟账户已存在"
            ) + f"，当前资金 {account.cash} 元。"
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
            text = f"模拟资金 {summary.account.cash} 元，持有 {summary.positions} 只证券。"
            if summary.market_value is None:
                text += f"行情不完整（{summary.priced}/{summary.positions}），总资产暂不可计算。"
            else:
                text += (
                    f"证券市值 {summary.market_value} 元，"
                    f"总资产 {summary.account.cash + summary.market_value} 元。"
                )
                if summary.oldest_quote:
                    oldest = summary.oldest_quote
                    if oldest.tzinfo is None:
                        oldest = oldest.replace(tzinfo=UTC)
                    age = (datetime.now(UTC) - oldest.astimezone(UTC)).total_seconds()
                    text += (
                        f"最早行情 {oldest.astimezone(CHINA):%Y-%m-%d %H:%M:%S}，"
                        f"来源 {PROVIDER_NAMES[provider]}"
                    )
                    if age < 0 or age > command.settings.max_quote_age_seconds:
                        text += "（已过期）"
                    text += "。"
            return text + ("账户状态待核实。" if summary.account.state != "active" else "")
        if command.action in {"positions", "orders"}:
            if len(args) > 1 or (args and (not args[0].isdigit() or not 1 <= int(args[0]) <= 1000)):
                return "请输入有效页码（1–1000）。"
            page = int(args[0]) if args else 1
            if self.accounts.get(user) is None:
                return "尚未开户，请先发送模拟开户指令。"
            if command.action == "positions":
                rows = self.accounts.positions(user, limit=25, offset=(page - 1) * 25)
                if not rows:
                    return "本页没有模拟持仓。"
                return "模拟持仓：\n" + "\n".join(
                    f"{row.current_code} {row.name}：{row.quantity} 股，"
                    f"可卖 {row.available} 股，成本 {row.total_cost} 元"
                    for row in rows
                )
            rows = self.orders.history(user, limit=25, offset=(page - 1) * 25)
            if not rows:
                return "本页没有模拟委托或成交。"
            return "模拟委托：\n" + "\n".join(
                f"{str(row.id)[:8]} {row.code} {'买入' if row.side == 'buy' else '卖出'} "
                f"{row.quantity} 股，{ORDER_STATUS.get(row.status, '待核实')}，"
                f"成交 {row.filled_quantity} 股，费用 {row.total_fees} 元，"
                f"报价来源 {PROVIDER_NAMES.get(row.quote_provider, row.quote_provider)}，"
                f"撮合来源 {PROVIDER_NAMES.get(row.execution_provider, row.execution_provider) if row.execution_provider else '尚未撮合'}"
                for row in rows
            )
        if command.action in {"buy", "sell"} and self.trade:
            return self.trade(service_id, command)
        if command.action == "backtest" and self.backtest:
            return self.backtest(service_id, command)
        return "量化服务尚未准备好，请稍后重试。"
