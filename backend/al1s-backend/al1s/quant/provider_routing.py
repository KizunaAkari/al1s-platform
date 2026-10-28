"""Select one compatible market source without mixing returned data."""

from collections.abc import Callable, Iterable
from typing import TypeVar

T = TypeVar("T")
PROVIDERS = ("sina", "tencent", "eastmoney", "tushare", "gugudata")
PROVIDER_NAMES = {
    "sina": "新浪",
    "tencent": "腾讯",
    "eastmoney": "东方财富",
    "tushare": "Tushare",
    "gugudata": "咕咕数据",
}
SOURCE_FAILURES = frozenset(
    {
        "quant_source_unavailable",
        "quant_source_empty",
        "quant_source_invalid",
        "quant_source_bar_invalid",
        "quant_source_duplicate_bar",
        "quant_source_units_unverified",
        "quant_quote_unavailable",
        "quant_quote_stale",
        "quant_market_data_missing",
        "quant_paper_bar_invalid",
        "quant_paper_bars_missing",
        "quant_bar_invalid",
        "quant_bar_duplicate",
        "quant_bar_nontrading_day",
    }
)


def eligible_sources(
    order: Iterable[str], *, market: str, frequency: str, adjustment: str, configured: set[str]
) -> tuple[str, ...]:
    if market not in {"SH", "SZ", "BJ"} or frequency not in {"1m", "1d"}:
        raise ValueError("quant_frequency_unsupported")
    if adjustment not in {"", "qfq", "hfq"} or (frequency == "1m" and adjustment):
        raise ValueError("quant_frequency_unsupported")
    result = []
    for provider in order:
        if provider not in PROVIDERS or provider in result:
            raise ValueError("quant_provider_invalid")
        if provider in {"tushare", "gugudata"} and provider not in configured:
            continue
        if market == "BJ" and (
            provider == "tencent" or (provider == "gugudata" and frequency == "1d")
        ):
            continue
        if adjustment and provider in {"sina", "tushare"}:
            continue
        result.append(provider)
    return tuple(result)


def select_source(order: Iterable[str], fetch: Callable[[str], T]) -> tuple[str, T]:
    for provider in order:
        try:
            return provider, fetch(provider)
        except ValueError as error:
            if str(error) not in SOURCE_FAILURES:
                raise
    raise ValueError("quant_source_unavailable")
