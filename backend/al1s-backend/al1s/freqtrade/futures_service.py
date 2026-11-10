from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from al1s.freqtrade.capital_service import CAPITAL_ACTIONS, execute_capital, parse_capital
from al1s.freqtrade.commands import parse_futures_order
from al1s.freqtrade.contracts import FreqtradeCommand
from al1s.freqtrade.leverage import leverage_limit
from al1s.freqtrade.manager import confirm_exit
from al1s.trading_replies.render import reply, text

if TYPE_CHECKING:
    from al1s.freqtrade.service import FreqtradeService


def execute_futures(
    service: "FreqtradeService", service_id: UUID, command: FreqtradeCommand, request_id: UUID
) -> str:
    if not command.settings.futures_enabled:
        return "合约模拟尚未启用，请管理员在Freqtrade合约页开启。"
    repo, manager = service.repository, service.manager
    user, args = command.source.sender_id, command.arguments
    if command.action == "f_open":
        if args:
            raise ValueError("合约开户不接受参数。")
        profile, created = repo.open(user, service_id, command.settings, market="futures")
        manager.ensure(profile, command.settings)
        return reply(command, "main" if created else "existing", dict(initial_cash=profile.initial_cash,
            status="已创建" if created else "已存在"), engine="freqtrade")
    profile = repo.profile(user, market="futures")
    if profile is None:
        return f"尚未合约开户，请先发送 {command.commands['f_open']}。"
    settings = (
        command.settings
        if profile.service_id == service_id
        else repo.get(profile.service_id).settings
    )
    parsed = None
    if command.action in CAPITAL_ACTIONS:
        parse_capital(command.action, args)
    if command.action in {"long", "short", "close", "f_deposit", "f_margin"}:
        if profile.state != "ready" or repo.blocking_request(user, request_id, market="futures"):
            return f"合约账户结果待核实，暂不能操作，请查询 {command.commands['f_orders']}。"
        if command.action not in CAPITAL_ACTIONS:
            parsed = parse_futures_order(command.action, args, settings)
    native = manager.ensure(profile, settings)
    if command.action in CAPITAL_ACTIONS:
        return execute_capital(service, native, command, request_id)
    if parsed is not None:
        return trade_futures(service, native, command, request_id, parsed, settings)
    return service.query(native, command, profile)


def trade_futures(service, native, command, request_id, parsed, settings) -> str:
    pair, amount, leverage = parsed
    try:
        rows = native.call("status")
    except (OSError, ValueError) as error:
        raise ValueError("无法核验合约持仓，本次未发送开仓或平仓委托，请稍后重试。") from error
    current = next((row for row in rows if row["pair"] == pair), None)
    if current is not None and current.get("has_open_orders"):
        raise ValueError("该合约有在途委托，请先查询合约成交。")
    if command.action in {"long", "short"}:
        is_short = command.action == "short"
        if current is not None:
            if bool(current["is_short"]) != is_short:
                raise ValueError("单向持仓不能同时做多做空，请先平仓再反手。")
            if len(command.arguments) == 2:
                leverage = int(current["leverage"])
            if Decimal(str(current["leverage"])) != leverage:
                raise ValueError(f"加仓须沿用当前{current['leverage']}倍杠杆；平仓后才能改倍数。")
        total_margin = amount + (Decimal(str(current["stake_amount"])) if current else 0)
        maximum = leverage_limit(pair, total_margin)
        if settings.futures_max_leverage:
            maximum = min(settings.futures_max_leverage, maximum)
        if not 1 <= leverage <= maximum:
            raise ValueError(f"按当前保证金和模拟档位，可用杠杆为1–{maximum}倍。")
        service.repository.prepare_native(request_id, current)
        result = native.call(
            "forceenter",
            {
                "pair": pair,
                "side": "short" if is_short else "long",
                "stakeamount": float(amount),
                "leverage": leverage,
                "ordertype": "market",
                "entry_tag": "al1s-" + request_id.hex,
            },
        )
        trade_id = result.get("trade_id")
        if not isinstance(trade_id, int):
            if str(result.get("status", "")).startswith("Error"):
                raise ValueError("框架未受理合约开仓，请检查保证金和最小交易金额。")
            raise OSError("freqtrade_futures_result_unconfirmed")
        if Decimal(str(result.get("leverage"))) != leverage:
            raise OSError("freqtrade_futures_leverage_unconfirmed")
        summary = f"{'开空' if is_short else '开多'}，保证金预算 {amount} USDT，{leverage}倍"
    else:
        if current is None:
            raise ValueError("没有该合约的可平仓持仓。")
        if amount is not None and amount > Decimal(str(current["amount"])):
            raise ValueError("平仓币量超过当前持仓。")
        service.repository.prepare_native(request_id, current)
        payload = {"tradeid": str(current["trade_id"]), "ordertype": "market"}
        if amount is not None:
            payload["amount"] = float(amount)
        confirm_exit(native.call("forceexit", payload), current["trade_id"])
        trade_id = current["trade_id"]
        summary = f"平仓 {amount if amount is not None else '全部'} 基础币"
    service.manager.market_success = datetime.now(UTC).isoformat()
    return reply(command, "main", dict(pair=pair, summary=summary, trade_id=trade_id,
        side={"long":"开多", "short":"开空", "close":"平仓"}[command.action],
        stake_amount=amount if command.action != "close" else None, leverage=leverage,
        amount_text=text(amount) if amount is not None else "全部",
        orders_command=command.commands["f_orders"]), engine="freqtrade")


def futures_help(command: FreqtradeCommand) -> str:
    w = command.commands
    maximum = command.settings.futures_max_leverage
    rule = f"配置上限{maximum}倍" if maximum else "上限随币对和保证金对应的原生模拟档位"
    return (
        f"\n\n【合约 · 开户与交易】\nUSDT逐仓永续模拟，合约钱包独立。\n{w['f_open']} — 合约开户，"
        f"初始 {text(command.settings.futures_initial_cash)} 虚拟USDT。\n"
        f"{w['long']} BTC 100 10 — 100 USDT保证金、10倍开多，名义预算约1000 USDT。\n"
        f"{w['short']} ETH 50 5 — 50 USDT保证金、5倍开空。\n"
        f"首次开仓省略倍数用默认{command.settings.futures_default_leverage}倍，{rule}。\n"
        f"{w['close']} BTC 0.001 — 按基础币数量部分平仓；{w['close']} BTC 全部 — 全平。\n"
        "同一合约不能同时多空，先平仓再反手；加仓省略倍数沿用原持仓，不在持仓中调整倍数。\n\n"
        f"【合约 · 查询】\n{w['f_balance']} — 合约资产。\n"
        f"{w['f_positions']} — 方向、杠杆、保证金、盈亏、资金费用与强平价。\n"
        f"{w['f_orders']} — 合约交易及退出原因。\n"
        f"持仓/成交每页10条；例：{w['f_orders']} 2，查询第2页。\n"
        "杠杆档位来自本版本模拟资料；实际数量、保证金和费用以框架成交为准，含原生强平保护退出。\n"
        "资金收付按原生记账符号：正数为收入，负数为支出。"
    )
