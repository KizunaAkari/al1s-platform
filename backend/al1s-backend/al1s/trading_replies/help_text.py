"""Pure help text shared by real replies and example previews."""

from al1s.trading_replies.render import text


def quant_help(command):
    words = dict(command.command_words)
    help_text = (
        "【A股模拟 · 量化使用帮助】\n仅供模拟交易和历史回测，不连接真实证券账户。\n"
        "在已授权的群聊或私聊发送以下指令，参数之间用空格分隔，可先@Bot。"
        "\n群聊回复对群成员可见。\n\n"
        f"【开户】\n{words['open']} — 创建自己的模拟账户，"
        f"初始虚拟资金 {text(command.settings.initial_cash)} 元；已有账户不会重置。\n\n"
        f"【买卖】\n{words['buy']} 600000 100 — 提交买入100股的模拟委托。\n"
        f"{words['sell']} 600000 100 — 提交卖出100股的模拟委托。\n"
        "格式：关键词 股票代码 股数；数量单位是股，不是手。\n"
        "已提交不代表已成交，请通过成交与委托查询核对。\n"
        "卖出受可卖数量和T+1限制；休市或行情无效时不能凭旧价成交。\n\n"
        f"【查询】\n{words['balance']} — 查看资金、市值与总资产。\n"
        f"{words['positions']} — 查看持仓、可卖股数与成本。\n"
        f"{words['orders']} — 查看委托状态、成交股数与费用。\n"
        f"持仓和成交默认第1页，每页25条；例如 {words['positions']} 2 或 "
        f"{words['orders']} 2 查看第2页（页码1–1000）。\n\n"
        f"【回测】\n{words['backtest']} 双均线 600000 2025-01-01 2025-12-31\n"
        "格式：关键词 策略 股票代码 起始日期 结束日期 [周期] [复权] [快线 慢线]。\n"
        "策略：买入持有、双均线；日期：YYYY-MM-DD。\n"
        "默认日线、不复权，双均线默认5/20；快慢线单位为所选周期的K线根数。\n"
        "可选周期：日线、1分钟；复权：不复权、前复权、后复权。\n"
        "1分钟仅支持不复权；快线和慢线仅用于双均线，需成对填写。\n"
        f"完整示例：{words['backtest']} 双均线 600000 "
        "2025-01-01 2025-12-31 日线 前复权 5 20\n"
        "回测也需先开户；可用区间以数据源实际返回为准，缺失历史不会补造。\n\n"
        f"【帮助】\n{words['help']} — 查看当前指令和用法。\n"
        f"{words['help']} 费率 — 查询当前佣金、最低佣金、卖出印花税和滑点。"
    )
    return help_text


def freqtrade_help(command):
    from al1s.freqtrade.futures_service import futures_help

    w = command.commands
    spot = (
        "【虚拟币模拟交易帮助】\n来源 Binance · 资金为虚拟USDT · 不连接真实交易账户\n"
        "参数用空格分隔；群聊回复对群成员可见。\n\n"
        f"【现货 · 开户与交易】\n{w['open']} — 创建本人独立账户，新账户 {text(command.settings.initial_cash)} USDT。\n"
        f"{w['buy']} BTC 100 — 花100 USDT买入BTC；也可用ETH、SOL或完整USDT币对。\n"
        f"{w['sell']} BTC 0.001 — 卖出0.001 BTC；{w['sell']} BTC 全部 — 卖出全部。\n"
        "数量与最小交易金额遵循交易所规则；已提交不代表已成交。\n\n"
        f"【现货 · 查询】\n{w['balance']} — 查询各币余额。\n{w['positions']} — 查询持仓。\n"
        f"{w['orders']} — 查询委托和成交。\n"
        f"持仓/成交每页20条；例：{w['positions']} 2，查询第2页。"
    )
    if "deposit" in w:
        spot += f"\n\n【现货 · 虚拟充值】\n{w['deposit']} 1000 — 给本人已有现货钱包充值1000虚拟USDT，保留原记录。"
    if not command.settings.futures_enabled:
        return (
            spot
            + (
                f"\n\n【价格走势】\n{w['chart']} BTC 1小时 — 查看现货价格走势图，无需开户。\n"
                "时间范围可省略，默认1小时；可选15分钟/1小时/4小时/1天/7天。"
                if "chart" in w
                else ""
            )
            + f"\n\n合约模拟当前未启用。\n{w['help']} — 查看当前指令与用法。"
        )
    charts = (
        f"\n\n【价格走势 · 无需开户】\n{w['chart']} BTC 1小时 — 现货价格走势图。\n"
        f"{w['f_chart']} BTC 4小时 — 合约价格走势图。\n"
        "时间范围可省略，默认1小时；可选15分钟/1小时/4小时/1天/7天。"
        if "chart" in w and "f_chart" in w
        else ""
    )
    capital = (
        f"\n\n【合约 · 虚拟资金】\n{w['f_deposit']} 1000 — 给本人合约钱包增加1000虚拟USDT。"
        f"\n{w['f_margin']} BTC 100 — 从可用余额划100 USDT给已有仓位，币数量不变；"
        "部分平仓后追加额保留，全部退出后释放。\n金额须为正、最多4位小数，"
        "每次最多10亿、累计充值最多1万亿虚拟USDT；不支持提现或自动开户。"
        if "f_deposit" in w and "f_margin" in w
        else ""
    )
    return (
        spot
        + futures_help(command)
        + capital
        + charts
        + "\n\n强平/保护平仓确认后在原开仓会话提醒，群内@本人。"
        + f"\n{w['help']} — 查看当前指令与用法。"
    )
