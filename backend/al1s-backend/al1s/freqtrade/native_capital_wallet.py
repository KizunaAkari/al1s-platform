from decimal import Decimal
from threading import RLock

from freqtrade.leverage.liquidation_price import update_liquidation_prices as _original_liquidation
from freqtrade.wallets import Wallet, Wallets

from al1s.freqtrade import native_capital_store as store

_original_init = Wallets.__init__
_original_update = Wallets._update_dry


def initialize_wallet(self, config, exchange, is_backtest=False):
    self._al1s_lock = RLock()
    self._al1s_seed = Decimal(str(config["dry_run_wallet"]))
    self._al1s_deposits = Decimal(0)
    self._al1s_margins = {}
    _original_init(self, config, exchange, is_backtest)


def update_wallet(self):
    with self._al1s_lock:
        self._al1s_deposits = store.cash_total()
        self._start_cap[self._stake_currency] = float(self._al1s_seed + self._al1s_deposits)
        _original_update(self)
        margins = store.open_margins() if self._config.get("trading_mode") == "futures" else {}
        extra = sum((row["extra"] for row in margins.values()), Decimal(0))
        currency = self._stake_currency
        balance = self._wallets[currency]
        self._wallets[currency] = Wallet(
            currency, balance.free - float(extra), balance.used + float(extra), balance.total
        )
        for row in margins.values():
            position = self._positions.get(row["pair"])
            if position is not None:
                self._positions[row["pair"]] = position._replace(
                    collateral=position.collateral + float(row["extra"])
                )
        self._al1s_margins = margins


def added_margin(wallets, trade):
    return getattr(wallets, "_al1s_margins", {}).get(trade.id, {}).get("extra", Decimal(0))


def stoploss_ratio(wallets, trade, fallback):
    extra = added_margin(wallets, trade)
    if not extra or trade.stake_amount <= 0:
        return fallback
    ratio = float((Decimal(str(trade.stake_amount)) + extra) / Decimal(str(trade.stake_amount)))
    return -ratio if trade.is_short else -min(ratio, trade.leverage or 1)


def update_liquidation(trade=None, *, exchange, wallets, stake_currency, dry_run=False):
    extra = added_margin(wallets, trade) if trade is not None else Decimal(0)
    if not extra or not trade.is_open:
        return _original_liquidation(
            trade,
            exchange=exchange,
            wallets=wallets,
            stake_currency=stake_currency,
            dry_run=dry_run,
        )
    with wallets._al1s_lock:
        trade.set_liquidation_price(
            exchange.get_liquidation_price(
                pair=trade.pair,
                open_rate=trade.open_rate,
                is_short=trade.is_short,
                amount=trade.amount,
                stake_amount=trade.stake_amount,
                leverage=trade.leverage,
                wallet_balance=float(Decimal(str(trade.stake_amount)) + extra),
            )
        )
        trade.adjust_stop_loss(
            trade.open_rate, stoploss_ratio(wallets, trade, -1), allow_refresh=True
        )


def wallet_state(wallets):
    balance = wallets._wallets[wallets._stake_currency]
    return dict(
        extension=1,
        deposits_total=str(wallets._al1s_deposits),
        free=balance.free,
        used=balance.used,
        total=balance.total,
        margins=[
            dict(
                trade_id=identity,
                pair=row["pair"],
                added_margin=str(row["extra"]),
                total_collateral=str(row["stake"] + row["extra"]),
            )
            for identity, row in wallets._al1s_margins.items()
        ],
    )
