import re
from decimal import Decimal
from string import Formatter
from types import SimpleNamespace

from al1s.trading_replies.catalog import COMMON, catalogue, definition, words


def text(value):
    if value is None:
        return "未提供"
    if isinstance(value, (Decimal, float)):
        number = Decimal(str(value))
        if not number.is_finite():
            return "未提供"
        if number == 0:
            return "0"
        value = format(number, "f")
        return value.rstrip("0").rstrip(".") if "." in value else value
    return str(value)


def fields(template, allowed):
    if not isinstance(template, str) or not 1 <= len(template) <= 4096 or not template.strip():
        raise ValueError("reply_template_invalid")
    found = set()
    try:
        for _, field, spec, conversion in Formatter().parse(template):
            if field is None:
                continue
            if not re.fullmatch(r"[a-z][a-z0-9_]*", field) or field not in allowed:
                raise ValueError("reply_variable_invalid:" + field)
            if spec or conversion is not None:
                raise ValueError("reply_template_format_invalid")
            found.add(field)
    except (ValueError, TypeError) as error:
        raise ValueError(str(error)) from error
    return found


def allowed(engine, action, variant):
    result = dict(definition(engine, action, variant)["variables"])
    if action == "help":
        result.update({key + "_command": "指令：" + value for key, value in words(engine).items()})
        result["initial_cash"] = "初始虚拟资金"
    return result


def validate_templates(engine, action, templates):
    if action not in catalogue(engine) or not isinstance(templates, dict):
        raise ValueError("reply_command_invalid")
    for variant, template in templates.items():
        fields(template, allowed(engine, action, variant))
    return dict(templates)


def render(engine, action, variant, values, templates=None):
    spec = definition(engine, action, variant)
    context = {key: text(value) for key, value in {**COMMON, **values}.items()}
    fallback_values = {key: "未提供" for key in allowed(engine, action, variant)} | context
    fallback = spec["default_template"].format_map(fallback_values)
    custom = (templates or {}).get(variant)
    if custom is None:
        return fallback
    try:
        required = fields(custom, allowed(engine, action, variant))
        result = (
            custom.format_map({**context, "default_reply": fallback})
            if required <= (context.keys() | {"default_reply"})
            else fallback
        )
        return result if result.strip() and len(result) <= 4096 else fallback
    except (ValueError, KeyError, TypeError):
        return fallback


def context(command, engine):
    mapping = dict(command.command_words)
    settings = command.settings
    futures = engine == "freqtrade" and getattr(command, "market", "spot") == "futures"
    return dict(
        qq_id=command.source.sender_id,
        trigger=mapping.get(command.action, ""),
        market_label="A股" if engine == "quant" else ("合约" if futures else "现货"),
        quote_currency="元" if engine == "quant" else "USDT",
        initial_cash=settings.futures_initial_cash if futures else settings.initial_cash,
        **{key + "_command": value for key, value in mapping.items()},
    )


def reply(command, variant, values, *, engine="quant"):
    overrides = getattr(command, "reply_templates", {}).get(command.action, {})
    return render(
        engine, command.action, variant, {**context(command, engine), **values}, overrides
    )


def render_list(engine, action, values, rows, templates=None, *, prefix=""):
    limit = 4096 - len(prefix)
    if rows:
        lines = [render(engine, action, "row", {**values, **row}, templates) for row in rows]
        result = render(engine, action, "main", {**values, "rows": "\n".join(lines)}, templates)
        if len(result) > limit:
            lines = [render(engine, action, "row", {**values, **row}) for row in rows]
            result = render(engine, action, "main", {**values, "rows": "\n".join(lines)})
    else:
        result = render(engine, action, "empty", values, templates)
        if len(result) > limit:
            result = render(engine, action, "empty", values)
    return prefix + (
        result if len(result) <= limit else "本页回复超过4096字符，请管理员精简本条指令的回复模板。"
    )


def list_reply(command, rows, values, *, engine="quant", prefix=""):
    return render_list(
        engine,
        command.action,
        {**context(command, engine), **values},
        rows,
        getattr(command, "reply_templates", {}).get(command.action, {}),
        prefix=prefix,
    )


def preview(
    engine, action, keyword, templates, variant, *, commands=None, initial_cash=None, settings=None
):
    validate_templates(engine, action, templates)
    spec = definition(engine, action, variant)
    mapping = {**words(engine), **(commands or {}), action: keyword}
    values = {
        **COMMON,
        **{key + "_command": value for key, value in mapping.items()},
        "trigger": keyword,
        "initial_cash": initial_cash or "1000",
        **spec["sample"],
    }
    values["market_label"] = (
        "A股"
        if engine == "quant"
        else ("合约" if catalogue(engine)[action]["group"] == "合约" else "现货")
    )
    values["quote_currency"] = "元" if engine == "quant" else "USDT"
    if action in {"open", "f_open"} and initial_cash is not None:
        values["initial_cash"] = initial_cash
    if action == "help":
        from al1s.freqtrade.contracts import FreqtradeSettings
        from al1s.quant.contracts import QuantSettings
        from al1s.trading_replies.help_text import freqtrade_help, quant_help

        settings = settings or (QuantSettings() if engine == "quant" else FreqtradeSettings())
        stub = SimpleNamespace(
            settings=settings, command_words=tuple(mapping.items()), commands=mapping
        )
        values["help_text"] = (quant_help if engine == "quant" else freqtrade_help)(stub)
        if variant == "fees":
            values.update(
                commission_percent=settings.commission_rate * 100,
                minimum_commission=settings.minimum_commission,
                tax_percent=settings.stamp_tax_rate * 100,
                slippage_percent=settings.slippage * 100,
            )
    if variant == "row" or (variant == "main" and "row" in catalogue(engine)[action]["variants"]):
        row = definition(engine, action, "row")
        values = {**definition(engine, action, "main")["sample"], **values}
        return render_list(engine, action, values, [row["sample"]], templates)
    return render(engine, action, variant, values, templates)
