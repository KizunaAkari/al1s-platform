from copy import deepcopy
from decimal import Decimal, InvalidOperation

from al1s.freqtrade.contracts import (
    CHART_COMMANDS,
    DEFAULT_COMMANDS,
    FUTURES_COMMANDS,
    PAIRS,
    SPOT_COMMANDS,
    FreqtradeCommand,
    FreqtradeRecord,
    FreqtradeSettings,
)
from al1s.quant.commands import normalize_keyword, parse_quant_event


def validate_words(words: dict[str, str], reserved: set[str]) -> dict[str, str]:
    if set(words) not in (
        set(DEFAULT_COMMANDS),
        set(SPOT_COMMANDS),
        set(SPOT_COMMANDS) | {"chart"},
        set(SPOT_COMMANDS) | set(FUTURES_COMMANDS),
        set(SPOT_COMMANDS) | set(FUTURES_COMMANDS) | set(CHART_COMMANDS),
        set(SPOT_COMMANDS) | {"chart", "deposit"},
    ):
        raise ValueError("freqtrade_commands_invalid")
    normalized = {key: normalize_keyword(value) for key, value in words.items()}
    if len(set(normalized.values())) != len(normalized) or set(normalized.values()) & reserved:
        raise ValueError("freqtrade_keyword_conflict")
    return normalized


def dispatch(record: FreqtradeRecord, event: object) -> FreqtradeCommand | None:
    settings = record.settings
    if not settings.enabled:
        return None
    parsed = parse_quant_event(
        event, record.commands, set(), record.row_version, allowed_actions=set(DEFAULT_COMMANDS)
    )
    if parsed is None:
        return None
    source = parsed.source
    if (source.scope_kind == "qq_group" and source.target_id not in settings.groups) or (
        source.scope_kind == "qq_private" and not settings.private_enabled
    ):
        return None
    return FreqtradeCommand(
        source=source,
        action=parsed.action,
        arguments=parsed.arguments,
        event_timestamp=parsed.event_timestamp,
        settings_version=record.row_version,
        mapping_version=record.row_version,
        normalized_keyword=parsed.normalized_keyword,
        settings=settings.model_copy(deep=True),
        commands=dict(record.commands),
        reply_templates=deepcopy(record.reply_templates),
    )


def parse_order(action: str, args: tuple[str, ...]) -> tuple[str, Decimal | None]:
    if action not in {"buy", "sell"} or len(args) != 2:
        raise ValueError("freqtrade_order_invalid")
    pair = args[0].upper()
    if "/" not in pair:
        pair += "/USDT"
    if pair not in PAIRS:
        raise ValueError("freqtrade_pair_invalid")
    if action == "sell" and args[1] == "全部":
        return pair, None
    try:
        amount = Decimal(args[1])
        if not amount.is_finite() or not 0 < amount <= 10**12 or amount.as_tuple().exponent < -12:
            raise ValueError("freqtrade_amount_invalid")
    except (InvalidOperation, ValueError) as error:
        raise ValueError("freqtrade_amount_invalid") from error
    return pair, amount


def parse_futures_order(action: str, args: tuple[str, ...], settings: FreqtradeSettings):
    if action not in {"long", "short", "close"} or len(args) not in (
        {2} if action == "close" else {2, 3}
    ):
        raise ValueError("合约参数格式错误，请查看币帮助。")
    symbol = args[0].upper()
    if symbol.endswith(":USDT"):
        symbol = symbol[:-5]
    pair, amount = parse_order("sell" if action == "close" else "buy", (symbol, args[1]))
    if action == "close":
        return pair + ":USDT", amount, None
    leverage = settings.futures_default_leverage
    if len(args) == 3:
        if not args[2].isascii() or not args[2].isdigit():
            raise ValueError("杠杆倍数必须是正整数。")
        leverage = int(args[2])
    maximum = settings.futures_max_leverage or 150
    if not 1 <= leverage <= maximum:
        raise ValueError(f"杠杆范围为1–{maximum}倍，并受币对原生模拟档位限制。")
    return pair + ":USDT", amount, leverage
