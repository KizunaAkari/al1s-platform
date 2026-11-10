from typing import ClassVar

from freqtrade.strategy import IStrategy


class ManualPaperStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1m"
    startup_candle_count = 0
    minimal_roi: ClassVar[dict[str, float]] = {}
    stoploss = -1.0
    trailing_stop = False
    use_exit_signal = False
    position_adjustment_enable = True
    max_entry_position_adjustment = -1

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_long"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe["exit_long"] = 0
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
        return exit_reason == "force_exit"
