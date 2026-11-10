from decimal import Decimal

from al1s.freqtrade.contracts import FUTURES_PAIRS, PAIRS, FreqtradeSettings, Market


def native_config(
    settings: FreqtradeSettings,
    *,
    initial_cash: Decimal,
    db_url: str,
    directory: str,
    port: int,
    password: str,
    market: Market = "spot",
) -> dict:
    futures = market == "futures"
    config = {
        "dry_run": True,
        "dry_run_wallet": float(initial_cash),
        "trading_mode": market,
        "stake_currency": "USDT",
        "stake_amount": "unlimited",
        "tradable_balance_ratio": 1,
        "max_open_trades": len(PAIRS),
        "timeframe": "1m",
        "fiat_display_currency": "",
        "fee": float(settings.futures_fee if futures else settings.fee),
        "db_url": db_url,
        "user_data_dir": directory,
        "strategy": "FuturesManualPaperStrategy" if futures else "ManualPaperStrategy",
        "strategy_path": directory,
        "initial_state": "running",
        "force_entry_enable": True,
        "minimal_roi": {},
        "stoploss": -1,
        "trailing_stop": False,
        "use_exit_signal": False,
        "position_adjustment_enable": True,
        "max_entry_position_adjustment": -1,
        "cancel_open_orders_on_exit": False,
        "unfilledtimeout": {"entry": 5, "exit": 5, "unit": "minutes"},
        "entry_pricing": {"price_side": "other", "use_order_book": True, "order_book_top": 1},
        "exit_pricing": {"price_side": "other", "use_order_book": True, "order_book_top": 1},
        "order_types": {
            "entry": "market",
            "exit": "market",
            "stoploss": "market",
            "stoploss_on_exchange": False,
        },
        "order_time_in_force": {"entry": "GTC", "exit": "GTC"},
        "exchange": {
            "name": "binance",
            "key": "",
            "secret": "",
            "enable_ws": False,
            "pair_whitelist": list(FUTURES_PAIRS if futures else PAIRS),
            "pair_blacklist": [],
            "ccxt_config": {
                "options": {"fetchMarkets": {"types": ["linear"] if futures else ["spot"]}}
            },
        },
        "pairlists": [{"method": "StaticPairList"}],
        "telegram": {"enabled": False, "token": "", "chat_id": ""},
        "api_server": {
            "enabled": True,
            "listen_ip_address": "127.0.0.1",
            "listen_port": port,
            "verbosity": "error",
            "enable_openapi": False,
            "username": "al1s",
            "password": password,
            "jwt_secret_key": password,
            "ws_token": password,
            "CORS_origins": [],
        },
        "internals": {"process_throttle_secs": 5, "heartbeat_interval": 60},
    }
    if futures:
        config["margin_mode"] = "isolated"
        config["liquidation_buffer"] = 0.05
    return config
