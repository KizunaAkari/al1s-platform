"""Apply a narrow entitlement API to the verified AKQuant 0.3.71 build source."""
from pathlib import Path

path = Path("/src/akquant-0.3.71/src/engine/python.rs")
source = path.read_text(encoding="utf-8")
anchor = '    /// 设置初始资金\n'
assert source.count(anchor) == 1
method = r'''
    /// Apply a verified paper-account dividend or bonus-share entitlement without creating a trade.
    fn apply_paper_entitlement(&mut self, symbol: String, shares: u64, cash: String, timestamp: i64) -> PyResult<()> {
        let cash_delta: Decimal = cash.parse().map_err(|_| PyValueError::new_err("invalid_cash"))?;
        if cash_delta < Decimal::ZERO || (shares == 0 && cash_delta == Decimal::ZERO) || timestamp <= 0 {
            return Err(PyValueError::new_err("invalid_entitlement"));
        }
        let instrument = self.instruments.get(&symbol).ok_or_else(|| PyValueError::new_err("unknown_instrument"))?;
        if instrument.asset_type != crate::model::AssetType::Stock || !self.state.order_manager.active_orders.is_empty() {
            return Err(PyValueError::new_err("unsupported_account_state"));
        }
        let quantity = Decimal::from(shares);
        let position = self.state.portfolio.positions.get(&symbol).copied().unwrap_or(Decimal::ZERO);
        let available = self.state.portfolio.available_positions.get(&symbol).copied().unwrap_or(Decimal::ZERO);
        let tracker = &self.state.order_manager.trade_tracker;
        let tracked: Decimal = tracker.long_inventory.get(&symbol).map(|rows| rows.iter().map(|row| row.quantity).sum()).unwrap_or(Decimal::ZERO);
        if position < Decimal::ZERO || available < Decimal::ZERO || available > position || tracked != position
            || tracker.short_inventory.get(&symbol).is_some_and(|rows| !rows.is_empty()) {
            return Err(PyValueError::new_err("position_mismatch"));
        }
        let owned: Vec<_> = self.strategy_positions.iter().filter_map(|(key, values)| {
            let qty = values.get(&symbol).copied().unwrap_or(Decimal::ZERO);
            if qty != Decimal::ZERO { Some((key.clone(), qty)) } else { None }
        }).collect();
        if owned.len() > 1 || owned.first().map(|(_, qty)| *qty).unwrap_or(Decimal::ZERO) != position {
            return Err(PyValueError::new_err("strategy_position_mismatch"));
        }
        let strategy = owned.first().map(|(key, _)| key.clone()).or_else(|| self.default_strategy_id.clone())
            .unwrap_or_else(|| "_default".to_string());
        let new_cash = self.state.portfolio.cash.checked_add(cash_delta).ok_or_else(|| PyValueError::new_err("cash_overflow"))?;
        let new_position = position.checked_add(quantity).ok_or_else(|| PyValueError::new_err("position_overflow"))?;
        let new_available = available.checked_add(quantity).ok_or_else(|| PyValueError::new_err("position_overflow"))?;
        let strategy_cash = self.strategy_cashflows.get(&strategy).copied().unwrap_or(Decimal::ZERO)
            .checked_add(cash_delta).ok_or_else(|| PyValueError::new_err("cash_overflow"))?;
        let equity = {
            let prices = self.last_prices.read().map_err(|_| PyValueError::new_err("price_lock"))?;
            calculate_account_metrics(&self.state.portfolio, &prices, &self.instruments,
                &self.state.order_manager.trade_tracker, &self.risk_manager.config).equity
        };
        self.state.portfolio.cash = new_cash;
        if shares > 0 {
            Arc::make_mut(&mut self.state.portfolio.positions).insert(symbol.clone(), new_position);
            Arc::make_mut(&mut self.state.portfolio.available_positions).insert(symbol.clone(), new_available);
            self.state.order_manager.trade_tracker.long_inventory.entry(symbol.clone()).or_default().push_back(
                crate::analysis::TradeEntry { quantity, price: Decimal::ZERO, multiplier: Decimal::ONE,
                    commission: Decimal::ZERO, bar_idx: self.bar_count, timestamp,
                    tag: "corporate_entitlement".to_string(), entry_portfolio_value: equity });
            self.strategy_positions.entry(strategy.clone()).or_default().insert(symbol, new_position);
        }
        self.strategy_cashflows.insert(strategy, strategy_cash);
        Ok(())
    }

'''
path.write_text(source.replace(anchor, method + anchor), encoding="utf-8")
