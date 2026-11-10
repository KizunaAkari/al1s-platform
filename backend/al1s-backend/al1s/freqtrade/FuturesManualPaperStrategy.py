from typing import ClassVar

from freqtrade.strategy import IStrategy


class FuturesManualPaperStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1m"
    startup_candle_count = 0
    minimal_roi: ClassVar[dict[str, float]] = {}
    stoploss = -1.0
    trailing_stop = False
    use_exit_signal = False
    can_short = True
    position_adjustment_enable = True
    max_entry_position_adjustment = -1

    def ft_stoploss_adjust(
        self,
        current_rate,
        trade,
        current_time,
        current_profit,
        force_stoploss,
        low=None,
        high=None,
        bound_profit=None,
        after_fill=False,
    ):
        from al1s.freqtrade.native_capital_wallet import (
            added_margin,
            stoploss_ratio,
            update_liquidation,
        )

        if trade.is_open and added_margin(self.wallets, trade):
            update_liquidation(
                trade,
                exchange=self.wallets._exchange,
                wallets=self.wallets,
                stake_currency="USDT",
                dry_run=True,
            )

        return super().ft_stoploss_adjust(
            current_rate,
            trade,
            current_time,
            current_profit,
            stoploss_ratio(self.wallets, trade, force_stoploss),
            low,
            high,
            bound_profit,
            after_fill,
        )

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def confirm_trade_exit(
        self,
        pair,
        trade,
        order_type,
        amount,
        rate,
        time_in_force,
        exit_reason,
        current_time,
        **kwargs,
    ):
        return exit_reason in {"force_exit", "liquidation", "stop_loss"}
