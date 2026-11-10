from decimal import Decimal
from functools import lru_cache
from types import SimpleNamespace


@lru_cache(maxsize=1)
def _paper_tiers():
    from freqtrade.enums import TradingMode
    from freqtrade.exchange.binance import Binance

    source = SimpleNamespace(trading_mode=TradingMode.FUTURES, _config={"dry_run": True})
    raw = Binance.load_leverage_tiers(source)
    return {
        pair: [Binance.parse_leverage_tier(None, tier) for tier in tiers]
        for pair, tiers in raw.items()
    }


def leverage_limit(pair: str, margin: Decimal) -> int:
    from freqtrade.enums import TradingMode
    from freqtrade.exceptions import InvalidOrderException, OperationalException
    from freqtrade.exchange.binance import Binance

    exchange = SimpleNamespace(trading_mode=TradingMode.FUTURES, _leverage_tiers=_paper_tiers())
    try:
        return int(Binance.get_max_leverage(exchange, pair, float(margin)))
    except (InvalidOrderException, OperationalException) as error:
        raise ValueError(f"无法确认该保证金的模拟杠杆档位：{error}") from error
