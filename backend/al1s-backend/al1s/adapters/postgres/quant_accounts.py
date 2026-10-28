"""Bounded, account-scoped reads and idempotent account creation."""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.quant_models import (
    QuantAccountRow,
    QuantInstrumentRow,
    QuantLotRow,
    QuantPositionRow,
    QuantQuoteRow,
)
from al1s.quant.market_rules import CHINA
from al1s.quant.provider_routing import PROVIDERS


@dataclass(frozen=True, slots=True)
class Account:
    id: UUID
    qq_user_id: str
    cash: Decimal
    initial_cash: Decimal
    state: str
    version: int
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Position:
    current_code: str
    market: str
    name: str
    quantity: int
    total_cost: Decimal
    available: int


@dataclass(frozen=True, slots=True)
class AccountSummary:
    account: Account
    positions: int
    priced: int
    market_value: Decimal | None
    oldest_quote: datetime | None


def _qq_id(qq_user_id: str) -> str:
    if not isinstance(qq_user_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", qq_user_id):
        raise ValueError("quant_qq_user_invalid")
    return qq_user_id


def _account(row: QuantAccountRow) -> Account:
    return Account(
        row.id, row.qq_user_id, row.cash, row.initial_cash, row.state, row.version, row.created_at
    )


class QuantAccounts:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def open(self, qq_user_id: str, initial_cash: Decimal) -> tuple[Account, bool]:
        qq_user_id = _qq_id(qq_user_id)
        if (
            not isinstance(initial_cash, Decimal)
            or not initial_cash.is_finite()
            or initial_cash <= 0
        ):
            raise ValueError("quant_initial_cash_invalid")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            created_id = session.scalar(
                insert(QuantAccountRow)
                .values(
                    id=uuid4(),
                    qq_user_id=qq_user_id,
                    initial_cash=initial_cash,
                    cash=initial_cash,
                    state="active",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=[QuantAccountRow.qq_user_id])
                .returning(QuantAccountRow.id)
            )
            row = session.scalar(
                select(QuantAccountRow).where(QuantAccountRow.qq_user_id == qq_user_id)
            )
            if row is None:
                raise RuntimeError("quant_account_creation_unavailable")
            return _account(row), created_id is not None

    def get(self, qq_user_id: str) -> Account | None:
        with self.sessions() as session:
            row = session.scalar(
                select(QuantAccountRow).where(QuantAccountRow.qq_user_id == _qq_id(qq_user_id))
            )
            return _account(row) if row else None

    def available_for(self, qq_user_id: str, instrument_id: UUID, day: date) -> int:
        if type(day) is not date:
            raise ValueError("quant_date_invalid")
        with self.sessions() as session:
            value = session.scalar(
                select(func.sum(QuantLotRow.remaining_quantity))
                .join(QuantPositionRow, QuantPositionRow.id == QuantLotRow.position_id)
                .join(QuantAccountRow, QuantAccountRow.id == QuantPositionRow.account_id)
                .where(
                    QuantAccountRow.qq_user_id == _qq_id(qq_user_id),
                    QuantPositionRow.instrument_id == instrument_id,
                    QuantLotRow.available_on <= day,
                )
            )
            return int(value or 0)

    def summary(self, qq_user_id: str, *, provider: str) -> AccountSummary | None:
        if provider not in PROVIDERS:
            raise ValueError("quant_provider_invalid")
        with self.sessions.begin() as session:
            account = session.scalar(
                select(QuantAccountRow)
                .where(QuantAccountRow.qq_user_id == _qq_id(qq_user_id))
                .with_for_update(read=True)
            )
            if account is None:
                return None
            row = session.execute(
                select(
                    func.count(QuantPositionRow.id),
                    func.count(QuantQuoteRow.instrument_id),
                    func.sum(QuantPositionRow.quantity * QuantQuoteRow.price),
                    func.min(QuantQuoteRow.quote_time),
                )
                .select_from(QuantPositionRow)
                .outerjoin(
                    QuantQuoteRow,
                    and_(
                        QuantQuoteRow.instrument_id == QuantPositionRow.instrument_id,
                        QuantQuoteRow.provider == provider,
                        QuantQuoteRow.price > 0,
                    ),
                )
                .where(QuantPositionRow.account_id == account.id, QuantPositionRow.quantity > 0)
            ).one()
            positions, priced, value, oldest = row
            market_value = (
                (Decimal(0) if positions == 0 else value) if priced == positions else None
            )
            return AccountSummary(_account(account), positions, priced, market_value, oldest)

    def positions(
        self, qq_user_id: str, *, limit: int = 25, offset: int = 0, as_of: date | None = None
    ) -> list[Position]:
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or offset < 0:
            raise ValueError("quant_pagination_invalid")
        day = as_of or datetime.now(CHINA).date()
        if type(day) is not date:
            raise ValueError("quant_date_invalid")
        with self.sessions.begin() as session:
            account_id = session.scalar(
                select(QuantAccountRow.id)
                .where(QuantAccountRow.qq_user_id == _qq_id(qq_user_id))
                .with_for_update(read=True)
            )
            if account_id is None:
                return []
            rows = session.execute(
                select(
                    QuantPositionRow.id,
                    QuantInstrumentRow.current_code,
                    QuantInstrumentRow.market,
                    QuantInstrumentRow.name,
                    QuantPositionRow.quantity,
                    QuantPositionRow.total_cost,
                )
                .join(QuantPositionRow, QuantPositionRow.instrument_id == QuantInstrumentRow.id)
                .where(QuantPositionRow.account_id == account_id, QuantPositionRow.quantity > 0)
                .order_by(
                    QuantInstrumentRow.market,
                    QuantInstrumentRow.current_code,
                    QuantInstrumentRow.id,
                )
                .limit(limit)
                .offset(offset)
            ).all()
            if not rows:
                return []
            available = dict(
                session.execute(
                    select(QuantLotRow.position_id, func.sum(QuantLotRow.remaining_quantity))
                    .where(
                        QuantLotRow.position_id.in_([row.id for row in rows]),
                        QuantLotRow.available_on <= day,
                    )
                    .group_by(QuantLotRow.position_id)
                ).all()
            )
            return [Position(*row[1:], int(available.get(row.id, 0))) for row in rows]
