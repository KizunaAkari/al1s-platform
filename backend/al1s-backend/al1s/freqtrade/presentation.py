from decimal import Decimal, InvalidOperation


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def position_values(row):
    margin, leverage = number(row.get("stake_amount")), number(row.get("leverage"))
    extra = number(row.get("added_margin", "0"))
    return dict(
        trade_id=row["trade_id"],
        pair=row["pair"],
        side="空" if row.get("is_short") else "多",
        amount=number(row.get("amount")),
        stake_amount=margin,
        leverage=leverage,
        added_margin=extra,
        total_collateral=margin + extra if margin is not None and extra is not None else None,
        notional=margin * leverage if margin is not None and leverage is not None else None,
        profit_abs=number(row.get("profit_abs")),
        funding_fees=number(row.get("funding_fees")),
        liquidation_price=number(row.get("liquidation_price")),
    )


def history_values(row):
    return dict(
        trade_id=row["trade_id"],
        pair=row["pair"],
        side="空" if row.get("is_short") else "多",
        leverage=number(row.get("leverage")),
        stake_amount=number(row.get("stake_amount")),
        profit_abs=number(
            row.get("profit_abs")
            if row.get("profit_abs") is not None
            else row.get("close_profit_abs")
        ),
        funding_fees=number(row.get("funding_fees")),
        exit_reason=row.get("exit_reason") or "持仓/委托中",
    )
