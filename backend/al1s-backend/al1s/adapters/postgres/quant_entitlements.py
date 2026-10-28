"""Apply recorded corporate rights and engine state in one transaction."""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.quant_models import (
    QuantAccountRow,
    QuantCashFlowRow,
    QuantCorporateEventRow,
    QuantEntitlementRow,
    QuantFillRow,
    QuantInstrumentRow,
    QuantLotRow,
    QuantOrderRow,
    QuantPositionRow,
)
from al1s.quant.corporate_actions import CorporateEvent, entitlement, payable
from al1s.quant.market_rules import CHINA


class QuantEntitlements:
    def __init__(self, sessions: sessionmaker[Session], adjust: Callable) -> None:
        self.sessions, self.adjust = sessions, adjust

    def capture(self, event_id: UUID, today: date) -> int:
        if type(today) is not date:
            raise ValueError("quant_date_invalid")
        with self.sessions.begin() as session:
            event = session.get(QuantCorporateEventRow, event_id, with_for_update=True)
            if event is None or event.state != "verified" or event.record_date is None:
                raise ValueError("quant_corporate_needs_review")
            if today <= event.record_date:
                raise ValueError("quant_record_date_not_closed")
            cutoff = datetime.combine(event.record_date + timedelta(days=1), time(0), tzinfo=CHINA)
            pending = session.scalar(
                select(QuantOrderRow.id)
                .where(
                    QuantOrderRow.instrument_id == event.instrument_id,
                    QuantOrderRow.event_time < int(cutoff.timestamp()),
                    QuantOrderRow.status.in_(
                        ("accepted", "processing", "partially_filled", "unknown")
                    ),
                )
                .limit(1)
            )
            if pending:
                raise ValueError("quant_corporate_needs_review")
            previous_pending = session.scalar(
                select(QuantEntitlementRow.id)
                .join(
                    QuantCorporateEventRow,
                    QuantCorporateEventRow.id == QuantEntitlementRow.event_id,
                )
                .where(
                    QuantCorporateEventRow.instrument_id == event.instrument_id,
                    QuantCorporateEventRow.id != event.id,
                    QuantCorporateEventRow.shares_date <= event.record_date,
                    QuantCorporateEventRow.shares_per_share > 0,
                    QuantEntitlementRow.shares_applied_at.is_(None),
                )
                .limit(1)
            )
            if previous_pending:
                raise ValueError("quant_corporate_needs_review")
            net = func.sum(
                case(
                    (QuantOrderRow.side == "buy", QuantFillRow.quantity),
                    else_=-QuantFillRow.quantity,
                )
            )
            rows = session.execute(
                select(QuantOrderRow.account_id, net)
                .join(QuantFillRow, QuantFillRow.order_id == QuantOrderRow.id)
                .where(
                    QuantOrderRow.instrument_id == event.instrument_id,
                    QuantOrderRow.event_time < int(cutoff.timestamp()),
                    QuantFillRow.occurred_at < cutoff,
                )
                .group_by(QuantOrderRow.account_id)
                .limit(10001)
            ).all()
            bonus = session.execute(
                select(
                    QuantEntitlementRow.account_id,
                    func.sum(
                        QuantEntitlementRow.eligible_quantity
                        * QuantCorporateEventRow.shares_per_share
                    ),
                )
                .join(
                    QuantCorporateEventRow,
                    QuantCorporateEventRow.id == QuantEntitlementRow.event_id,
                )
                .where(
                    QuantCorporateEventRow.instrument_id == event.instrument_id,
                    QuantCorporateEventRow.id != event.id,
                    QuantCorporateEventRow.shares_date <= event.record_date,
                    QuantEntitlementRow.shares_applied_at.is_not(None),
                )
                .group_by(QuantEntitlementRow.account_id)
                .limit(10001)
            ).all()
            if len(rows) > 10000 or len(bonus) > 10000:
                raise ValueError("quant_corporate_account_limit")
            quantities = {account_id: Decimal(amount) for account_id, amount in rows}
            for account_id, amount in bonus:
                quantities[account_id] = quantities.get(account_id, Decimal(0)) + amount
            if any(qty < 0 or qty != qty.to_integral_value() for qty in quantities.values()):
                raise ValueError("quant_corporate_needs_review")
            values = [
                {
                    "id": uuid4(),
                    "account_id": account_id,
                    "event_id": event.id,
                    "eligible_quantity": int(qty),
                    "state": "verified",
                }
                for account_id, qty in quantities.items()
                if qty > 0
            ]
            inserted = 0
            for start in range(0, len(values), 500):
                stmt = insert(QuantEntitlementRow).values(values[start : start + 500])
                result = session.scalars(
                    stmt.on_conflict_do_nothing(
                        index_elements=[
                            QuantEntitlementRow.account_id,
                            QuantEntitlementRow.event_id,
                        ]
                    ).returning(QuantEntitlementRow.id)
                ).all()
                inserted += len(result)
            event.captured_at = datetime.now(UTC)
            return inserted

    def apply(self, right_id: UUID, today: date) -> bool:
        if type(today) is not date:
            raise ValueError("quant_date_invalid")
        with self.sessions.begin() as session:
            identity = session.execute(
                select(QuantEntitlementRow.account_id, QuantEntitlementRow.event_id).where(
                    QuantEntitlementRow.id == right_id
                )
            ).first()
            if identity is None:
                raise ValueError("quant_entitlement_not_found")
            account_id, event_id = identity
            event = session.get(QuantCorporateEventRow, event_id, with_for_update=True)
            account = session.get(QuantAccountRow, account_id, with_for_update=True)
            right = session.get(QuantEntitlementRow, right_id, with_for_update=True)
            if account is None or right is None or account.state != "active":
                raise ValueError("quant_account_needs_review")
            instrument = session.get(QuantInstrumentRow, event.instrument_id) if event else None
            if (
                event is None
                or instrument is None
                or right.state != "verified"
                or event.state != "verified"
            ):
                raise ValueError("quant_corporate_needs_review")
            value = CorporateEvent(
                event.record_date,
                event.cash_date,
                event.shares_date,
                event.cash_per_share,
                event.shares_per_share,
                True,
            )
            rights = entitlement(value, record_date_quantity=right.eligible_quantity)
            cash, shares = payable(
                value,
                rights,
                today,
                cash_done=right.cash_applied_at is not None,
                shares_done=right.shares_applied_at is not None,
            )
            if not cash and not shares:
                return False
            if session.scalar(
                select(QuantOrderRow.id)
                .where(
                    QuantOrderRow.account_id == account_id,
                    QuantOrderRow.status.in_(
                        ("accepted", "processing", "partially_filled", "unknown")
                    ),
                )
                .limit(1)
            ):
                raise ValueError("quant_order_pending")
            if not account.engine_state or account.engine_version != "0.3.71":
                raise ValueError("quant_engine_state_invalid")
            position = session.scalar(
                select(QuantPositionRow)
                .where(
                    QuantPositionRow.account_id == account_id,
                    QuantPositionRow.instrument_id == instrument.id,
                )
                .with_for_update()
            )
            before = position.quantity if position else 0
            available = (
                (
                    session.scalar(
                        select(func.sum(QuantLotRow.remaining_quantity)).where(
                            QuantLotRow.position_id == position.id,
                            QuantLotRow.available_on <= today,
                        )
                    )
                    or 0
                )
                if position
                else 0
            )
            effective_days = ([event.cash_date] if cash else []) + (
                [event.shares_date] if shares else []
            )
            applied_at = datetime.combine(max(effective_days), time(0), tzinfo=CHINA)
            adjusted = self.adjust(
                account.engine_state, instrument.current_code, shares, cash, applied_at
            )
            if (
                adjusted.cash != account.cash + cash
                or adjusted.position != before + shares
                or adjusted.available != available + shares
                or not adjusted.state
            ):
                raise ValueError("quant_engine_mismatch")
            now = datetime.now(UTC)
            if shares:
                if position is None:
                    position = QuantPositionRow(
                        id=uuid4(),
                        account_id=account_id,
                        instrument_id=instrument.id,
                        quantity=0,
                        total_cost=Decimal(0),
                        updated_at=now,
                    )
                    session.add(position)
                    session.flush()
                position.quantity += shares
                position.updated_at = now
                session.add(
                    QuantLotRow(
                        id=uuid4(),
                        position_id=position.id,
                        acquired_on=event.shares_date,
                        available_on=event.shares_date,
                        remaining_quantity=shares,
                        remaining_cost=Decimal(0),
                    )
                )
                right.shares_applied_at = now
            if cash:
                session.add(
                    QuantCashFlowRow(
                        id=uuid4(),
                        account_id=account_id,
                        entitlement_id=right.id,
                        kind="dividend",
                        amount=cash,
                        occurred_at=datetime.combine(event.cash_date, time(0), tzinfo=CHINA),
                    )
                )
                right.cash_applied_at = now
            account.cash, account.engine_state = adjusted.cash, adjusted.state
            account.version, account.updated_at = account.version + 1, now
            return True
