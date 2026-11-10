"""Run with the pinned Freqtrade interpreter inside the existing QQ container."""

import importlib.util
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

if importlib.util.find_spec("freqtrade") is None:
    raise unittest.SkipTest("requires the pinned Freqtrade interpreter")

from ccxt import TICK_SIZE
from freqtrade.enums import ExitType, MarginMode, TradingMode
from freqtrade.exchange.binance import Binance
from freqtrade.persistence import Trade, init_db
from freqtrade.wallets import Wallets
from sqlalchemy import event

from al1s.freqtrade.FuturesManualPaperStrategy import FuturesManualPaperStrategy
from al1s.freqtrade.native_capital import CapitalOperation, apply_operation, install
from al1s.freqtrade.native_capital_store import operation_record
from al1s.freqtrade.native_capital_wallet import _original_update, update_liquidation, wallet_state


class CapitalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install()

    def setUp(self):
        init_db("sqlite://")
        self.exchange = SimpleNamespace(
            get_proxy_coin=lambda: "USDT",
            get_pair_base_currency=lambda pair: pair.split("/")[0],
            trading_mode=TradingMode.FUTURES,
            margin_mode=MarginMode.ISOLATED,
            get_maintenance_ratio_and_amt=lambda *args: (0.004, 0),
        )

        def liquidation(**values):
            return max(
                0, Binance.dry_run_liquidation_price(self.exchange, open_trades=[], **values)
            )

        self.exchange.get_liquidation_price = liquidation
        self.config = dict(
            dry_run=True,
            dry_run_wallet=1000,
            stake_currency="USDT",
            trading_mode=TradingMode.FUTURES,
            margin_mode=MarginMode.ISOLATED,
            exchange={"name": "binance"},
        )
        self.wallets = Wallets(self.config, self.exchange)
        self.bot = SimpleNamespace(
            config=self.config, wallets=self.wallets, exchange=self.exchange, _exit_lock=Lock()
        )

    def tearDown(self):
        Trade.session.remove()

    def position(self, short=False):
        trade = Trade(
            pair="BTC/USDT:USDT",
            exchange="binance",
            is_open=True,
            is_short=short,
            stake_amount=100,
            amount=0.125,
            open_rate=80000,
            open_date=datetime.now(UTC),
            leverage=100,
            fee_open=0.0005,
            fee_close=0.0005,
            trading_mode=TradingMode.FUTURES,
            price_precision=0.1,
            precision_mode_price=TICK_SIZE,
        )
        trade.recalc_open_trade_value()
        Trade.session.add(trade)
        Trade.commit()
        self.wallets.update()
        update_liquidation(
            trade, exchange=self.exchange, wallets=self.wallets, stake_currency="USDT", dry_run=True
        )
        Trade.commit()
        return trade

    def operation(self, action, amount, trade_id=None, operation_id=None):
        return CapitalOperation(
            operation_id=operation_id or uuid4(),
            action=action,
            amount=Decimal(amount),
            trade_id=trade_id,
        )

    def test_deposit_replay_parameter_conflict_and_restore(self):
        op = self.operation("deposit", "500")
        first = apply_operation(self.bot, op)
        assert first == apply_operation(self.bot, op)
        assert self.wallets.get_free("USDT") == 1500
        with self.assertRaises(ValueError):
            apply_operation(self.bot, op.model_copy(update={"amount": Decimal("600")}))
        restored = Wallets(self.config, self.exchange)
        assert restored.get_free("USDT") == 1500

    def test_margin_does_not_change_position_or_absolute_pnl_for_long_and_short(self):
        for short in (False, True):
            with self.subTest(short=short):
                trade = self.position(short)
                original = (
                    trade.amount,
                    trade.open_rate,
                    trade.leverage,
                    trade.stake_amount,
                    trade.calc_profit(80100),
                )
                old = trade.liquidation_price
                apply_operation(self.bot, self.operation("margin", "50", trade.id))
                assert original == (
                    trade.amount,
                    trade.open_rate,
                    trade.leverage,
                    trade.stake_amount,
                    trade.calc_profit(80100),
                )
                assert (trade.liquidation_price > old) if short else (trade.liquidation_price < old)
                assert self.wallets.get_used("USDT") == 150
                trade.is_open = False
                trade.close_profit_abs = 0
                Trade.commit()
                self.wallets.update()
                assert self.wallets.get_used("USDT") == 0

    def test_partial_exit_keeps_added_margin_and_full_exit_releases_it(self):
        trade = self.position()
        apply_operation(self.bot, self.operation("margin", "50", trade.id))
        trade.amount /= 2
        trade.stake_amount /= 2
        Trade.commit()
        self.wallets.update()
        assert self.wallets.get_used("USDT") == 100
        assert wallet_state(self.wallets)["margins"][0]["added_margin"] == "50.0000"
        trade.is_open = False
        trade.close_profit_abs = 0
        Trade.commit()
        self.wallets.update()
        assert self.wallets.get_used("USDT") == 0

    def test_insufficient_free_rejects_without_capital_effect(self):
        trade = self.position()
        with self.assertRaises(ValueError):
            apply_operation(self.bot, self.operation("margin", "901", trade.id))
        self.wallets.update()
        assert self.wallets.get_free("USDT") == 900
        assert wallet_state(self.wallets)["margins"] == []

    def test_transaction_failure_rolls_back_money_and_protection_price(self):
        trade = self.position()
        before = trade.liquidation_price
        op = self.operation("margin", "50", trade.id)
        with patch(
            "al1s.freqtrade.native_capital_store.save_operation", side_effect=RuntimeError("fail")
        ), self.assertRaises(RuntimeError):
            apply_operation(self.bot, op)
        assert operation_record(op.operation_id) is None
        assert self.wallets.get_free("USDT") == 900
        assert Trade.session.get(Trade, trade.id).liquidation_price == before

    def test_lost_ack_after_commit_does_not_repeat_deposit(self):
        op = self.operation("deposit", "500")
        original = Trade.commit

        def lost_ack():
            original()
            raise OSError("ack lost")

        with patch.object(Trade, "commit", side_effect=lost_ack), self.assertRaises(OSError):
            apply_operation(self.bot, op)
        assert operation_record(op.operation_id)["result"]["amount"] == "500.0000"
        apply_operation(self.bot, op)
        assert self.wallets.get_free("USDT") == 1500

    def test_old_stoploss_does_not_trigger_after_added_collateral(self):
        strategy = FuturesManualPaperStrategy(self.config)
        strategy.wallets = self.wallets
        for short in (False, True):
            trade = self.position(short)
            trade.adjust_stop_loss(trade.open_rate, -1, initial=True)
            old = trade.liquidation_price
            apply_operation(self.bot, self.operation("margin", "50", trade.id))
            new = trade.liquidation_price
            halfway = (old + new) / 2
            check = strategy.ft_stoploss_reached(halfway, trade, datetime.now(UTC), 0, 0)
            assert check.exit_type == ExitType.NONE, check
            crossed = new + 1 if short else new - 1
            check = strategy.ft_stoploss_reached(crossed, trade, datetime.now(UTC), 0, 0)
            assert check.exit_type.value in ("liquidation", "stop_loss"), check
            trade.is_open = False
            trade.close_profit_abs = 0
            Trade.commit()
            self.wallets.update()

    def test_stale_core_protection_fields_are_refreshed_before_exit_check(self):
        strategy = FuturesManualPaperStrategy(self.config)
        strategy.wallets = self.wallets
        trade = self.position()
        trade.adjust_stop_loss(trade.open_rate, -1, initial=True)
        old_liquidation, old_stop = trade.liquidation_price, trade.stop_loss
        apply_operation(self.bot, self.operation("margin", "50", trade.id))
        new = trade.liquidation_price
        trade.liquidation_price, trade.stop_loss = old_liquidation, old_stop
        check = strategy.ft_stoploss_reached(
            (old_liquidation + new) / 2, trade, datetime.now(UTC), 0, 0
        )
        assert check.exit_type == ExitType.NONE, check

    def test_zero_extension_matches_native_wallet(self):
        self.position()
        _original_update(self.wallets)
        before = (dict(self.wallets._wallets), dict(self.wallets._positions))
        self.wallets.update()
        assert before == (self.wallets._wallets, self.wallets._positions)

    def test_live_mode_and_other_versions_are_rejected(self):
        op = self.operation("deposit", "100")
        self.bot.config = {**self.config, "dry_run": False}
        with self.assertRaises(ValueError):
            apply_operation(self.bot, op)
        with (
            patch("freqtrade.__version__", "other"),
            patch("al1s.freqtrade.native_capital._installed", False),self.assertRaises(ValueError)
        ):
            install()

    def test_added_margin_metadata_uses_one_batch_for_three_open_positions(self):
        for pair in ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"):
            trade = self.position()
            trade.pair = pair
            Trade.commit()
            apply_operation(self.bot, self.operation("margin", "10", trade.id))
        statements = []
        engine = Trade.session.get_bind()

        def count(connection, cursor, statement, parameters, context, executemany):
            if "trade_custom_data" in statement:
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", count)
        try:
            self.wallets.update()
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert len(statements) == 1, len(statements)
        assert len(wallet_state(self.wallets)["margins"]) == 3


if __name__ == "__main__":
    unittest.main()
