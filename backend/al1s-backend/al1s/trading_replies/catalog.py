"""Fixed reply fields supplied by the two existing paper-trading services."""

from al1s.freqtrade.contracts import DEFAULT_COMMANDS as FT_WORDS
from al1s.quant.commands import DEFAULT_COMMANDS as QUANT_WORDS

LABELS = dict(
    qq_id="QQ号",
    trigger="当前触发词",
    market_label="市场",
    quote_currency="计价单位",
    default_reply="系统默认回复",
    status="中文状态",
    heading="结果标题",
    cash="账户现金",
    initial_cash="初始虚拟资金",
    stock_name="股票名称",
    stock_label="股票名称与代码",
    code="股票代码",
    side="买卖方向",
    quantity="委托股数",
    reference_price="参考价",
    order_id="短委托号",
    orders_command="成交查询指令",
    pending="未成交说明",
    position_count="持仓数",
    priced_count="已估值数",
    market_value="证券市值",
    total_assets="总资产",
    quote_info="行情说明",
    account_warning="账户核实提示",
    page="页码",
    rows="记录列表",
    name="证券名称",
    available="可卖股数",
    total_cost="总成本",
    filled_quantity="已成交股数",
    total_fees="费用",
    quote_provider="报价来源",
    execution_provider="撮合来源",
    strategy="策略",
    frequency="周期",
    start="开始日期",
    end="结束日期",
    ma_info="均线说明",
    fast="快线",
    slow="慢线",
    return_percent="收益率(%)",
    drawdown_percent="最大回撤(%)",
    executions="成交次数",
    provider="行情来源",
    adjustment="复权口径",
    digest="数据摘要",
    commission_percent="佣金(%)",
    minimum_commission="最低佣金(元)",
    tax_percent="卖出印花税(%)",
    slippage_percent="滑点(%)",
    backtest_warning="回测口径说明",
    help_text="完整指令用法",
    pair="交易币对",
    trade_id="原生交易编号",
    amount="基础币数量",
    amount_text="本次数量或预算说明",
    fee_percent="模拟费率(%)",
    summary="操作说明",
    leverage="原开仓杠杆",
    stake_amount="开仓保证金或成本",
    currency="资产币种",
    free="可用余额",
    balance="总余额",
    used="占用保证金",
    capital_summary="充值和追加占用说明",
    deposits_total="累计虚拟充值",
    added_margin="追加保证金",
    total_collateral="总保证金",
    notional="持仓名义金额",
    profit_abs="原生盈亏",
    funding_fees="累计资金收付",
    liquidation_price="模拟强平/保护价",
    unrealized_pnl="未实现盈亏合计",
    state="中文交易状态",
    last_order="最近委托明细",
    exit_reason="退出原因",
    symbol="币种",
    period="走势时间范围",
    interval="K线周期",
    range_start="实际起点",
    range_end="实际终点",
    latest_close="最新完整收盘价",
    change_percent="区间变化(%)",
    latest_bar_time="完整K线时间",
    collected_at="采集时间",
    free_after="操作后可用余额",
    capital_amount="本次充值或追加额",
    added_total="追加合计",
)

COMMON = dict(qq_id="123456789", trigger="示例指令", market_label="现货", quote_currency="USDT")


def variant(label, template, sample):
    return {
        "label": label,
        "default_template": template,
        "variables": {key: LABELS.get(key, key) for key in {*COMMON, *sample, "default_reply"}},
        "sample": sample,
    }


def command(label, group, description, variants):
    return {"label": label, "group": group, "description": description, "variants": variants}


def _quant():
    stock = dict(
        heading="买入委托已受理",
        stock_name="宁德时代",
        stock_label="宁德时代（300750）",
        code="300750",
        side="买入",
        quantity=1000,
        reference_price="225.8",
        status="已受理",
        order_id="6a91e8d3",
        pending="尚未成交；",
        orders_command="模拟成交",
    )
    trade = variant(
        "委托结果",
        "【A股模拟 · {heading}】\n{stock_label} · {quantity}股\n"
        "参考价：{reference_price}元\n委托号：{order_id}\n"
        "{pending}发送 {orders_command} 查询实际成交。",
        stock,
    )
    return {
        "open": command(
            "开户",
            "A股",
            "创建本人模拟账户，已有账户保留。",
            {
                "main": variant(
                    "新开户",
                    "【A股模拟 · 开户成功】\n模拟账户已创建\n当前资金：{cash} 元",
                    {"cash": "100000", "status": "已创建"},
                ),
                "existing": variant(
                    "已有账户",
                    "【A股模拟 · 已有账户】\n模拟账户已存在\n当前资金：{cash} 元\n资金与交易记录保持，不重复发放初始资金。",
                    {"cash": "99000", "status": "已存在"},
                ),
            },
        ),
        "buy": command(
            "买入", "A股", "按股票代码和股数提交买入委托；参考价不是成交价。", {"main": trade}
        ),
        "sell": command(
            "卖出",
            "A股",
            "按股票代码和股数提交卖出委托，保留T+1限制。",
            {"main": {**trade, "sample": {**stock, "heading": "卖出委托已受理", "side": "卖出"}}},
        ),
        "balance": command(
            "资产",
            "A股",
            "查看账户现金与已有行情能够估值的资产。",
            {
                "main": variant(
                    "可估值",
                    "【A股模拟 · 资产】\n模拟资金：{cash} 元\n证券市值：{market_value} 元\n"
                    "总资产：{total_assets} 元\n持仓：{position_count} 只证券\n"
                    "{quote_info}{account_warning}",
                    dict(
                        cash="82000",
                        position_count=1,
                        priced_count=1,
                        market_value="22580",
                        total_assets="104580",
                        quote_info="最早行情 2026-10-29 09:33:00，来源 新浪。",
                        account_warning="",
                    ),
                ),
                "incomplete": variant(
                    "行情不完整",
                    "【A股模拟 · 资产】\n模拟资金：{cash} 元\n持仓：{position_count} 只证券\n"
                    "行情不完整（{priced_count}/{position_count}），总资产暂不可计算。\n{account_warning}",
                    dict(cash="82000", position_count=2, priced_count=1, account_warning=""),
                ),
            },
        ),
        "positions": command(
            "持仓",
            "A股",
            "分页查看已有持仓和可卖数量。",
            {
                "main": variant("持仓列表", "【A股模拟持仓 · 第{page}页】\n{rows}", dict(page=1, rows="")),
                "row": variant(
                    "单条持仓",
                    "{code} {name}\n持有 {quantity} 股 · 可卖 {available} 股 · 总成本 {total_cost} 元\n",
                    dict(
                        code="300750",
                        name="宁德时代",
                        quantity=100,
                        available=100,
                        total_cost="22000",
                    ),
                ),
                "empty": variant("空页", "【A股模拟持仓 · 第{page}页】\n本页没有模拟持仓。", dict(page=1)),
            },
        ),
        "orders": command(
            "成交与委托",
            "A股",
            "分页查询真实委托状态和已成交数量。",
            {
                "main": variant("委托列表", "【A股模拟委托 · 第{page}页】\n{rows}", dict(page=1, rows="")),
                "row": variant(
                    "单条委托",
                    "{code} {side} · {status} · 委托 {order_id}\n"
                    "委托 {quantity} 股 · 已成交 {filled_quantity} 股 · 费用 {total_fees} 元\n"
                    "报价来源：{quote_provider} · 撮合来源：{execution_provider}\n",
                    dict(
                        order_id="6a91e8d3",
                        code="300750",
                        side="买入",
                        quantity=100,
                        status="已成交",
                        filled_quantity=100,
                        total_fees="5",
                        quote_provider="新浪",
                        execution_provider="新浪",
                    ),
                ),
                "empty": variant("空页", "【A股模拟委托 · 第{page}页】\n本页没有模拟委托或成交。", dict(page=1)),
            },
        ),
        "backtest": command(
            "回测",
            "A股",
            "输出现有回测结果及数据口径，不改变策略。",
            {
                "main": variant(
                    "回测完成",
                    "【A股模拟 · 回测完成】\n{strategy} · {code} · {frequency}{ma_info}\n"
                    "区间：{start} 至 {end}\n收益率：{return_percent}%\n"
                    "最大回撤：{drawdown_percent}% · 成交：{executions} 次\n\n"
                    "数据：{provider} · {adjustment} · 摘要 {digest}\n"
                    "佣金 {commission_percent}% · 每笔最低 {minimum_commission} 元\n"
                    "卖出税 {tax_percent}% · 滑点 {slippage_percent}%\n"
                    "每次买入按可用资金的75%估算，结果仅为模拟。\n{backtest_warning}",
                    dict(
                        strategy="双均线",
                        code="300750",
                        frequency="日线",
                        start="2025-01-01",
                        end="2025-12-31",
                        fast=5,
                        slow=20,
                        ma_info="，均线 5/20",
                        return_percent="12.35",
                        drawdown_percent="6.2",
                        executions=8,
                        provider="新浪",
                        adjustment="不复权",
                        digest="123456abcdef",
                        commission_percent="0.01",
                        minimum_commission=5,
                        tax_percent="0.05",
                        slippage_percent=0,
                        backtest_warning="不复权回测未计入分红送转，收益可能失真。",
                    ),
                ),
                "status": variant(
                    "已有任务状态",
                    "【A股模拟 · 回测状态】\n任务：{order_id}\n状态：{status}",
                    dict(order_id="1234abcd", status="处理中"),
                ),
            },
        ),
        "help": command(
            "帮助",
            "通用",
            "正常用法和费率表分别设置，帮助词保持动态。",
            {
                "main": variant(
                    "完整用法",
                    "{help_text}",
                    dict(help_text="量化使用帮助\n模拟买入 300750 100 — 提交买入100股。"),
                ),
                "fees": variant(
                    "费率表",
                    "模拟交易费率（当前配置）\n佣金：{commission_percent}%（买卖均收）\n"
                    "每笔最低佣金：{minimum_commission}元\n印花税：{tax_percent}%（仅卖出）\n滑点：{slippage_percent}%\n\n"
                    "历史委托按提交时保存的费率执行，当前设置用于后续新委托。",
                    dict(
                        commission_percent="0.0001",
                        minimum_commission=5,
                        tax_percent="0.05",
                        slippage_percent=0,
                    ),
                ),
            },
        ),
    }


def _freqtrade():
    result = {}
    for futures, prefix, group in [(False, "", "现货"), (True, "f_", "合约")]:
        market = "逐仓永续" if futures else "虚拟币"
        result[prefix + "open"] = command(
            group + "开户",
            group,
            "每QQ独立模拟钱包，已有账户不会重置。",
            {
                "main": variant(
                    "新开户",
                    f"【{group}模拟 · 开户成功】\n{market}模拟账户已创建\n"
                    "初始虚拟资金：{initial_cash} USDT\n"
                    + ("与现货钱包独立。" if futures else "与A股、合约钱包独立。"),
                    dict(initial_cash="1000", status="已创建"),
                ),
                "existing": variant(
                    "已有账户",
                    f"【{group}模拟 · 已有账户】\n{market}模拟账户已存在\n"
                    "开户初始资金：{initial_cash} USDT（非当前余额）\n资金与交易记录保持，不重复发放初始资金。"
                    + ("与现货钱包独立。" if futures else ""),
                    dict(initial_cash="1000", status="已存在"),
                ),
            },
        )
        balance_text = (
            "【逐仓合约模拟资产】\n{rows}\n"
            "持仓未实现盈亏：{unrealized_pnl} USDT（原生估值合计）{capital_summary}\n"
            "来源：Binance；USDT余额含充值/追加占用，不作为实时总权益。"
            if futures
            else "【现货模拟资产】\n{rows}{capital_summary}\n来源：Binance；数量单位按资产币种，不折算为总权益。"
        )
        totals = dict(
            rows="",
            capital_summary="\n累计虚拟充值 1000 USDT；追加保证金占用 0 USDT。",
            deposits_total="1000",
            added_margin="0",
            unrealized_pnl="35.2",
        )
        if not futures:
            totals.pop("unrealized_pnl")
        result[prefix + "balance"] = command(
            group + "资产",
            group,
            "按币种显示原生余额，不把余额当作实时总权益。",
            {
                "main": variant("资产列表", balance_text, totals),
                "empty": variant("无资产", balance_text, {**totals, "rows": "暂无资产。"}),
                "row": variant(
                    "单个币种",
                    "{currency}：可用 {free} · 总量 {balance}"
                    + (" · 占用保证金 {used}" if futures else ""),
                    dict(
                        currency="USDT",
                        free="900",
                        balance="1000",
                        **({"used": "100"} if futures else {}),
                    ),
                ),
            },
        )
        position = (
            "#{trade_id} {pair} · {side} · 原开仓{leverage}倍\n"
            "数量 {amount} 币 · 名义金额 {notional} USDT\n"
            "开仓保证金 {stake_amount} + 追加 {added_margin} = 总保证金 {total_collateral} USDT\n"
            "浮盈亏 {profit_abs} USDT · 累计资金收付 {funding_fees} USDT\n"
            "模拟强平/保护价：{liquidation_price} USDT\n"
            if futures
            else "#{trade_id} {pair}\n数量：{amount} 币 · 成本：{stake_amount} USDT\n"
        )
        position_values = dict(
            trade_id=1,
            pair="BTC/USDT:USDT" if futures else "BTC/USDT",
            amount="0.01",
            stake_amount="100",
        )
        if futures:
            position_values.update(
                side="多",
                leverage=10,
                added_margin=20,
                total_collateral=120,
                notional=1000,
                profit_abs="35.2",
                funding_fees="-0.03",
                liquidation_price="70300",
            )
        result[prefix + "positions"] = command(
            group + "持仓",
            group,
            "分页显示现有持仓；单条模板控制每行。",
            {
                "main": variant("持仓列表", f"【{group}模拟持仓 · 第{{page}}页】\n{{rows}}", dict(page=1, rows="")),
                "empty": variant("空页", f"【{group}模拟持仓 · 第{{page}}页】\n本页没有持仓。", dict(page=1)),
                "row": variant("单条持仓", position, position_values),
            },
        )
        history = (
            "#{trade_id} {pair} · {side} · {leverage}倍 · {state}\n"
            "开仓保证金 {stake_amount} USDT · 原生盈亏 {profit_abs} USDT\n"
            "累计资金收付 {funding_fees} USDT · 退出原因：{exit_reason}\n{last_order}\n"
            if futures
            else "#{trade_id} {pair} · {state}\n{last_order}\n"
        )
        order_values = dict(
            trade_id=1,
            pair=position_values["pair"],
            state="已平仓",
            last_order="最近委托 closed，成交 0.01 币",
        )
        if futures:
            order_values.update(
                side="多",
                leverage=10,
                stake_amount=100,
                profit_abs="35.2",
                funding_fees="-0.03",
                exit_reason="force_exit",
            )
        result[prefix + "orders"] = command(
            group + "成交",
            group,
            "查询原生委托/成交；账户恢复提示由系统保留。",
            {
                "main": variant("交易列表", f"【{group}模拟交易记录 · 第{{page}}页】\n{{rows}}", dict(page=1, rows="")),
                "empty": variant("空页", f"【{group}模拟交易记录 · 第{{page}}页】\n本页没有交易记录。", dict(page=1)),
                "row": variant("单条交易", history, order_values),
            },
        )
        result[prefix + "chart"] = command(
            group + "走势",
            group,
            "只编辑成功生成图片后的随图文字。",
            {
                "main": variant(
                    "随图文字",
                    "【{market_label}走势 · {symbol}/USDT · 最近{period}】\n"
                    "区间：{range_start} 至 {range_end}\n"
                    "最新完整收盘价：{latest_close} USDT · 区间变化：{change_percent}%\n"
                    "来源：{provider} · 完整K线 · 周期 {interval}\n"
                    "行情时间：{latest_bar_time}\n采集时间：{collected_at}（北京时间）",
                    dict(
                        symbol="BTC",
                        pair=position_values["pair"],
                        market_label=group,
                        period="1小时",
                        interval="1m",
                        range_start="10-29 09:00",
                        range_end="10-29 10:00",
                        latest_close="80500",
                        change_percent="1.2",
                        latest_bar_time="2026-10-29 10:00:00",
                        collected_at="2026-10-29 10:01:00",
                        provider="Binance",
                    ),
                )
            },
        )
        result[prefix + "deposit"] = command(
            group + "充值",
            group,
            "只格式化原生已确认入金结果。",
            {
                "main": variant(
                    "充值成功",
                    "【{market_label}模拟 · 充值成功】\n本次充值：{capital_amount} 虚拟USDT\n"
                    "可用余额：{free_after} USDT\n已计入本人钱包，原资金和交易记录保留。",
                    dict(capital_amount=1000, free_after=1900, market_label=group),
                )
            },
        )
    for action, side in [("buy", "买入"), ("sell", "卖出")]:
        result[action] = command(
            side,
            "现货",
            "买入参数为USDT预算，卖出参数为币数量或全部。",
            {
                "main": variant(
                    "委托提交",
                    "【现货模拟 · {side}委托已提交】\n{pair} · 交易 #{trade_id}\n"
                    + ("买入预算：{amount_text}\n" if action == "buy" else "卖出数量：{amount_text}\n")
                    + "提交不代表成交，发送 {orders_command} 核对。\n来源：Binance · 模拟费率：{fee_percent}%",
                    dict(
                        pair="BTC/USDT",
                        side=side,
                        trade_id=1,
                        amount_text="100 USDT" if action == "buy" else "全部",
                        orders_command="币成交",
                        fee_percent="0.1",
                    ),
                )
            },
        )
    for action, label in [("long", "开多"), ("short", "开空"), ("close", "平仓")]:
        result[action] = command(
            "合约" + label,
            "合约",
            "保证金/数量为指令预算，实际成交以原生记录为准。",
            {
                "main": variant(
                    "委托提交",
                    "【逐仓合约模拟 · {side}委托已提交】\n{pair} · 交易 #{trade_id}\n{summary}\n"
                    "提交不代表成交；实际数量和保证金以原生成交为准。\n发送 {orders_command} 核对。",
                    dict(
                        pair="BTC/USDT:USDT",
                        summary="平仓 全部 基础币"
                        if action == "close"
                        else f"{label}，保证金预算 100 USDT，10倍",
                        side=label,
                        amount_text="全部",
                        trade_id=1,
                        orders_command="合约成交",
                        **({"stake_amount": 100, "leverage": 10} if action != "close" else {}),
                    ),
                )
            },
        )
    result["f_margin"] = command(
        "追加保证金",
        "合约",
        "原生确认后展示追加额和保护价，不改变仓位数量。",
        {
            "main": variant(
                "追加成功",
                "【逐仓合约模拟 · 保证金追加成功】\n{pair}\n本次追加：{capital_amount} 虚拟USDT\n"
                "追加合计：{added_total} USDT · 总保证金：{total_collateral} USDT\n"
                "原生模拟保护价：{liquidation_price} USDT\n可用余额：{free_after} USDT\n"
                "币数量不变；部分平仓后追加额保留，全部退出后解除占用。",
                dict(
                    pair="BTC/USDT:USDT",
                    capital_amount=100,
                    added_total=120,
                    total_collateral=220,
                    liquidation_price=70000,
                    free_after=800,
                ),
            )
        },
    )
    result["help"] = command(
        "帮助",
        "通用",
        "完整用法随当前指令词及账户设置更新。",
        {
            "main": variant(
                "完整用法",
                "{help_text}",
                dict(help_text="虚拟币模拟交易帮助\n币买入 BTC 100 — 使用100 USDT预算买入。"),
            )
        },
    )
    return result


CATALOGUES = {"quant": _quant(), "freqtrade": _freqtrade()}


def catalogue(engine):
    try:
        return CATALOGUES[engine]
    except KeyError as error:
        raise ValueError("reply_engine_invalid") from error


def words(engine):
    return QUANT_WORDS if engine == "quant" else FT_WORDS


def arguments_example(engine, action):
    if engine == "quant":
        return {
            "buy": "300750 100",
            "sell": "300750 100",
            "positions": "[页码]",
            "orders": "[页码]",
            "backtest": "双均线 300750 2025-01-01 2025-12-31",
            "help": "[费率]",
        }.get(action, "")
    return {
        "buy": "BTC 100",
        "sell": "BTC 0.01 / BTC 全部",
        "long": "BTC 100 10",
        "short": "ETH 50 5",
        "close": "BTC 0.01 / BTC 全部",
        "positions": "[页码]",
        "orders": "[页码]",
        "f_positions": "[页码]",
        "f_orders": "[页码]",
        "chart": "BTC [1小时]",
        "f_chart": "BTC [4小时]",
        "deposit": "1000",
        "f_deposit": "1000",
        "f_margin": "BTC 100",
    }.get(action, "")


def definition(engine, action, key):
    try:
        return catalogue(engine)[action]["variants"][key]
    except KeyError as error:
        raise ValueError("reply_variant_invalid") from error
