"""Persist manual order identities before handing work to the paper engine."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.meme_base import fingerprint
from al1s.adapters.postgres.quant_accounts import _qq_id
from al1s.adapters.postgres.quant_corporate_sync import QuantCorporateSync
from al1s.adapters.postgres.quant_models import (
    QuantAccountRow,
    QuantCashFlowRow,
    QuantFillRow,
    QuantInstrumentRow,
    QuantLotRow,
    QuantOrderRow,
    QuantPositionRow,
)
from al1s.quant.book import Fill, Lot, apply_fill
from al1s.quant.commands import QuantCommand
from al1s.quant.market_rules import CHINA, check_quote
from al1s.quant.paper_engine import PaperInstrument


@dataclass(frozen=True, slots=True)
class Order:
    id: UUID
    status: str
    side: str
    quantity: int
    provider: str | None = None
    quote_time: datetime | None = None
    quote_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OrderHistory:
    id: UUID
    code: str
    side: str
    quantity: int
    status: str
    created_at: datetime
    filled_quantity: int
    total_fees: Decimal
    quote_provider: str
    execution_provider: str | None


@dataclass(frozen=True, slots=True)
class PendingOrder:
    id: UUID
    account_id: UUID
    instrument_id: UUID
    market: str
    symbol: str
    board: str
    side: str
    quantity: int
    event_time: int
    status: str
    commission_rate: Decimal
    minimum_commission: Decimal
    stamp_tax_rate: Decimal
    slippage: Decimal
    provider: str
    quote_max_age_seconds: int = 120
    refresh_seconds: int = 60
    created_at: datetime | None = None
    provider_order: tuple[str, ...] = ()
    execution_provider: str | None = None


@dataclass(frozen=True, slots=True)
class PaperSnapshot:
    order: PendingOrder
    initial_cash: Decimal
    cash: Decimal
    state: bytes | None
    positions: dict[str, int]
    instruments: tuple[PaperInstrument, ...]


def _pending(order: QuantOrderRow, instrument: QuantInstrumentRow) -> PendingOrder:
    return PendingOrder(
        order.id,
        order.account_id,
        instrument.id,
        instrument.market,
        instrument.current_code,
        instrument.board,
        order.side,
        order.quantity,
        order.event_time,
        order.status,
        order.commission_rate,
        order.minimum_commission,
        order.stamp_tax_rate,
        order.slippage,
        order.provider,
        order.quote_max_age_seconds,
        order.refresh_seconds,
        order.created_at,
        tuple(order.provider_order),
        order.execution_provider,
    )


def _order(row: QuantOrderRow) -> Order:
    return Order(
        row.id, row.status, row.side, row.quantity, row.provider, row.quote_time, row.quote_price
    )


class QuantOrders:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        enforce_corporate: bool = True,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sessions = sessions
        self.enforce_corporate = enforce_corporate
        self.now = now

    def pending(
        self, *, limit: int = 50, after: tuple[datetime, UUID] | None = None
    ) -> list[PendingOrder]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("quant_pagination_invalid")
        with self.sessions() as session:
            query = (
                select(QuantOrderRow, QuantInstrumentRow)
                .join(QuantInstrumentRow, QuantInstrumentRow.id == QuantOrderRow.instrument_id)
                .where(QuantOrderRow.status == "accepted")
            )
            if after is not None:
                query = query.where(
                    or_(
                        QuantOrderRow.created_at > after[0],
                        (QuantOrderRow.created_at == after[0]) & (QuantOrderRow.id > after[1]),
                    )
                )
            rows = session.execute(
                query.order_by(QuantOrderRow.created_at, QuantOrderRow.id).limit(limit)
            ).all()
            return [_pending(order, instrument) for order, instrument in rows]

    def prepare(
        self,
        order_id: UUID,
        *,
        quote_time: datetime | None = None,
        execution_provider: str | None = None,
    ) -> PaperSnapshot:
        with self.sessions.begin() as session:
            identity = session.execute(
                select(QuantOrderRow.account_id, QuantOrderRow.instrument_id).where(
                    QuantOrderRow.id == order_id
                )
            ).first()
            if identity is None:
                raise ValueError("quant_order_not_found")
            account_id, instrument_id = identity
            instrument = session.get(
                QuantInstrumentRow, instrument_id, with_for_update={"read": True}
            )
            if instrument is None:
                raise ValueError("quant_instrument_unavailable")
            account = session.get(QuantAccountRow, account_id, with_for_update=True)
            order = session.get(QuantOrderRow, order_id, with_for_update=True)
            if account is None or order is None or account.state != "active":
                raise ValueError("quant_account_needs_review")
            if order.status != "accepted":
                raise ValueError("quant_order_state_conflict")
            chosen = execution_provider or order.provider
            if chosen not in order.provider_order:
                raise ValueError("quant_provider_invalid")
            if account.engine_state and account.engine_version != "0.3.71":
                raise ValueError("quant_engine_state_invalid")
            if self.enforce_corporate:
                created = (
                    account.created_at
                    if account.created_at.tzinfo
                    else account.created_at.replace(tzinfo=UTC)
                )
                checked_at = self.now()
                if quote_time is None:
                    raise ValueError("quant_quote_unavailable")
                check_quote(
                    Decimal(1),
                    quote_time,
                    checked_at,
                    {checked_at.astimezone(CHINA).date()},
                    order.quote_max_age_seconds,
                )
                QuantCorporateSync.check_ready_in(
                    session,
                    instrument_id,
                    account.id,
                    since=created.astimezone(CHINA).date(),
                    now=checked_at,
                )
            rows = session.execute(
                select(
                    QuantInstrumentRow.current_code,
                    QuantInstrumentRow.board,
                    QuantPositionRow.quantity,
                )
                .join(QuantInstrumentRow, QuantInstrumentRow.id == QuantPositionRow.instrument_id)
                .where(QuantPositionRow.account_id == account.id, QuantPositionRow.quantity > 0)
                .order_by(QuantInstrumentRow.current_code)
                .limit(1001)
            ).all()
            if len(rows) > 1000:
                raise ValueError("quant_position_limit")
            positions = {code: qty for code, _board, qty in rows}
            boards = {code: board for code, board, _qty in rows}
            positions.setdefault(instrument.current_code, 0)
            boards.setdefault(instrument.current_code, instrument.board)
            order.execution_provider, order.status, order.updated_at = (
                chosen,
                "processing",
                datetime.now(UTC),
            )
            return PaperSnapshot(
                _pending(order, instrument),
                account.initial_cash,
                account.cash,
                account.engine_state,
                positions,
                tuple(PaperInstrument(code, board) for code, board in boards.items()),
            )

    def expire(self, order_id: UUID) -> None:
        with self.sessions.begin() as session:
            row = session.get(QuantOrderRow, order_id, with_for_update=True)
            if row is None or row.status != "accepted":
                raise ValueError("quant_order_state_conflict")
            row.status, row.updated_at = "expired", datetime.now(UTC)

    def mark_unknown(self, order_id: UUID) -> bool:
        with self.sessions.begin() as session:
            account_id = session.scalar(
                select(QuantOrderRow.account_id).where(QuantOrderRow.id == order_id)
            )
            if account_id is None:
                raise ValueError("quant_order_not_found")
            account = session.get(QuantAccountRow, account_id, with_for_update=True)
            row = session.get(QuantOrderRow, order_id, with_for_update=True)
            if account is None or row is None:
                raise ValueError("quant_order_not_found")
            if row.status in {"filled", "rejected", "expired", "unknown"}:
                return False
            if row.status not in {"processing", "partially_filled"}:
                raise ValueError("quant_order_state_conflict")
            now = datetime.now(UTC)
            row.status, row.updated_at = "unknown", now
            account.state, account.updated_at, account.version = (
                "needs_review",
                now,
                account.version + 1,
            )
            return True

    def history(self, qq_user_id: str, *, limit: int = 25, offset: int = 0) -> list[OrderHistory]:
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or offset < 0:
            raise ValueError("quant_pagination_invalid")
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
                    QuantOrderRow.id,
                    QuantInstrumentRow.current_code,
                    QuantOrderRow.side,
                    QuantOrderRow.quantity,
                    QuantOrderRow.status,
                    QuantOrderRow.created_at,
                    QuantOrderRow.provider,
                    QuantOrderRow.execution_provider,
                )
                .join(QuantInstrumentRow, QuantInstrumentRow.id == QuantOrderRow.instrument_id)
                .where(QuantOrderRow.account_id == account_id)
                .order_by(QuantOrderRow.created_at.desc(), QuantOrderRow.id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            if not rows:
                return []
            fills = {
                item.order_id: (int(item.quantity), item.fees)
                for item in session.execute(
                    select(
                        QuantFillRow.order_id,
                        func.sum(QuantFillRow.quantity).label("quantity"),
                        func.sum(QuantFillRow.fees).label("fees"),
                    )
                    .where(QuantFillRow.order_id.in_([row.id for row in rows]))
                    .group_by(QuantFillRow.order_id)
                ).all()
            }
            return [
                OrderHistory(
                    *row[:6],
                    *fills.get(row.id, (0, Decimal(0))),
                    row.provider,
                    row.execution_provider,
                )
                for row in rows
            ]

    def start(self, order_id: UUID) -> Order:
        with self.sessions.begin() as session:
            account_id = session.scalar(
                select(QuantOrderRow.account_id).where(QuantOrderRow.id == order_id)
            )
            if account_id is None:
                raise ValueError("quant_order_not_found")
            account = session.get(QuantAccountRow, account_id, with_for_update=True)
            row = session.get(QuantOrderRow, order_id, with_for_update=True)
            if account is None or row is None or account.state != "active":
                raise ValueError("quant_account_needs_review")
            if row.status != "accepted":
                raise ValueError("quant_order_state_conflict")
            row.execution_provider, row.status, row.updated_at = (
                row.provider,
                "processing",
                datetime.now(UTC),
            )
            return _order(row)

    def complete(
        self,
        order_id: UUID,
        fill: Fill,
        *,
        next_trading_day: date,
        engine_cash: Decimal,
        engine_position: int,
        engine_state: bytes,
        engine_version: str,
        occurred_at: datetime,
    ) -> Order:
        if not isinstance(engine_state, bytes) or not engine_state or engine_version != "0.3.71":
            raise ValueError("quant_engine_state_invalid")
        if (
            not isinstance(occurred_at, datetime)
            or occurred_at.tzinfo is None
            or occurred_at.astimezone(CHINA).date() != fill.trade_date
        ):
            raise ValueError("quant_fill_time_invalid")
        with self.sessions.begin() as session:
            account_id = session.scalar(
                select(QuantOrderRow.account_id).where(QuantOrderRow.id == order_id)
            )
            if account_id is None:
                raise ValueError("quant_order_not_found")
            account = session.get(QuantAccountRow, account_id, with_for_update=True)
            order = session.get(QuantOrderRow, order_id, with_for_update=True)
            if account is None or order is None or account.state != "active":
                raise ValueError("quant_account_needs_review")
            if order.status != "processing":
                raise ValueError("quant_order_state_conflict")
            if (
                order.side != fill.side
                or order.quantity != fill.quantity
                or (account.engine_version is not None and account.engine_version != engine_version)
            ):
                raise ValueError("quant_fill_mismatch")
            self._commit_full_fill(
                session,
                account,
                order,
                fill,
                next_trading_day,
                engine_cash,
                engine_position,
                engine_state,
                engine_version,
                occurred_at,
            )
            return _order(order)

    @staticmethod
    def _commit_full_fill(
        session: Session,
        account: QuantAccountRow,
        order: QuantOrderRow,
        fill: Fill,
        next_trading_day: date,
        engine_cash: Decimal,
        engine_position: int,
        engine_state: bytes,
        engine_version: str,
        occurred_at: datetime,
    ) -> None:
        position = session.scalar(
            select(QuantPositionRow)
            .where(
                QuantPositionRow.account_id == account.id,
                QuantPositionRow.instrument_id == order.instrument_id,
            )
            .with_for_update()
        )
        if position is None and fill.side == "sell":
            raise ValueError("quant_insufficient_available")
        rows = (
            []
            if position is None
            else list(
                session.scalars(
                    select(QuantLotRow)
                    .where(
                        QuantLotRow.position_id == position.id, QuantLotRow.remaining_quantity > 0
                    )
                    .order_by(QuantLotRow.acquired_on, QuantLotRow.available_on, QuantLotRow.id)
                    .with_for_update()
                ).all()
            )
        )
        lots = [
            Lot(
                row.acquired_on,
                row.available_on,
                row.remaining_quantity,
                row.remaining_cost,
                row.id,
            )
            for row in rows
        ]
        if position is not None and (
            position.quantity != sum(lot.quantity for lot in lots)
            or position.total_cost != sum((lot.cost for lot in lots), Decimal(0))
        ):
            raise ValueError("quant_ledger_mismatch")
        result = apply_fill(
            account.cash,
            lots,
            fill,
            next_trading_day=next_trading_day,
            engine_cash=engine_cash,
            engine_position=engine_position,
        )
        now = datetime.now(UTC)
        if position is None:
            position = QuantPositionRow(
                id=uuid4(),
                account_id=account.id,
                instrument_id=order.instrument_id,
                quantity=0,
                total_cost=Decimal(0),
                updated_at=now,
            )
            session.add(position)
            session.flush()
        updated = {lot.id: lot for lot in result.lots if lot.id is not None}
        for row in rows:
            lot = updated.get(row.id)
            row.remaining_quantity = lot.quantity if lot else 0
            row.remaining_cost = lot.cost if lot else Decimal(0)
        for lot in result.lots:
            if lot.id is None:
                session.add(
                    QuantLotRow(
                        id=uuid4(),
                        position_id=position.id,
                        acquired_on=lot.acquired_on,
                        available_on=lot.available_on,
                        remaining_quantity=lot.quantity,
                        remaining_cost=lot.cost,
                    )
                )
        position.quantity = sum(lot.quantity for lot in result.lots)
        position.total_cost = sum((lot.cost for lot in result.lots), Decimal(0))
        position.updated_at = now
        fill_id = uuid4()
        session.add(
            QuantFillRow(
                id=fill_id,
                order_id=order.id,
                sequence=1,
                quantity=fill.quantity,
                price=fill.price,
                fees=fill.fees,
                occurred_at=occurred_at,
            )
        )
        session.flush()
        session.add(
            QuantCashFlowRow(
                id=uuid4(),
                account_id=account.id,
                order_id=order.id,
                fill_id=fill_id,
                kind=fill.side,
                amount=result.cash - account.cash,
                occurred_at=occurred_at,
            )
        )
        account.cash, account.engine_state, account.engine_version = (
            result.cash,
            engine_state,
            engine_version,
        )
        account.version, account.updated_at = account.version + 1, now
        order.status, order.updated_at = "filled", now
        session.flush()

    def recover_interrupted(self) -> int:
        """Call once during exclusive worker startup, before accepting new work."""
        interrupted = ("processing", "partially_filled")
        with self.sessions.begin() as session:
            account_ids = select(QuantOrderRow.account_id).where(
                QuantOrderRow.status.in_(interrupted)
            )
            session.execute(
                update(QuantAccountRow)
                .where(QuantAccountRow.id.in_(account_ids))
                .values(
                    state="needs_review",
                    updated_at=datetime.now(UTC),
                    version=QuantAccountRow.version + 1,
                )
            )
            result = session.execute(
                update(QuantOrderRow)
                .where(QuantOrderRow.status.in_(interrupted))
                .values(status="unknown", updated_at=datetime.now(UTC))
            )
            return result.rowcount

    def accept(
        self,
        service_id: UUID,
        command: QuantCommand,
        instrument_id: UUID,
        quantity: int,
        *,
        quote_time: datetime | None = None,
        quote_price: Decimal | None = None,
        quote_provider: str | None = None,
    ) -> tuple[Order, bool]:
        if (
            command.action not in {"buy", "sell"}
            or type(quantity) is not int
            or quantity <= 0
            or len(command.arguments) != 2
            or not command.arguments[1].isdecimal()
            or int(command.arguments[1]) != quantity
            or command.settings is None
            or command.settings_version < 1
            or (
                quote_provider is not None and quote_provider not in command.settings.provider_order
            )
        ):
            raise ValueError("quant_order_invalid")
        source = command.source
        identity = (
            service_id,
            source.scope_kind,
            source.target_id,
            source.sender_id,
            source.message_id,
            command.event_timestamp,
        )
        digest = fingerprint(
            {
                "action": command.action,
                "arguments": command.arguments,
                "instrument_id": str(instrument_id),
                "quantity": quantity,
                "mapping_version": command.mapping_version,
                "settings_version": command.settings_version,
                "settings": command.settings.model_dump(mode="json"),
            }
        )
        with self.sessions.begin() as session:
            instrument = session.get(
                QuantInstrumentRow, instrument_id, with_for_update={"read": True}
            )
            if instrument is None or not instrument.active:
                raise ValueError("quant_instrument_unavailable")
            account = session.scalar(
                select(QuantAccountRow)
                .where(QuantAccountRow.qq_user_id == source.sender_id)
                .with_for_update()
            )
            if account is None:
                raise ValueError("quant_account_not_found")
            existing = session.scalar(
                select(QuantOrderRow).where(
                    QuantOrderRow.service_id == identity[0],
                    QuantOrderRow.scope_kind == identity[1],
                    QuantOrderRow.target_id == identity[2],
                    QuantOrderRow.sender_id == identity[3],
                    QuantOrderRow.message_id == identity[4],
                    QuantOrderRow.event_time == identity[5],
                )
            )
            if existing is not None:
                if existing.request_sha256 != digest:
                    raise ValueError("quant_event_conflict")
                return _order(existing), False
            if account.state != "active":
                raise ValueError("quant_account_needs_review")
            if self.enforce_corporate:
                created = (
                    account.created_at
                    if account.created_at.tzinfo
                    else account.created_at.replace(tzinfo=UTC)
                )
                checked_at = self.now()
                if quote_time is None or quote_price is None:
                    raise ValueError("quant_quote_unavailable")
                check_quote(
                    quote_price,
                    quote_time,
                    checked_at,
                    {checked_at.astimezone(CHINA).date()},
                    command.settings.max_quote_age_seconds,
                )
                QuantCorporateSync.check_ready_in(
                    session,
                    instrument_id,
                    account.id,
                    since=created.astimezone(CHINA).date(),
                    now=checked_at,
                )
            pending = session.scalar(
                select(QuantOrderRow.id)
                .where(
                    QuantOrderRow.account_id == account.id,
                    QuantOrderRow.status.in_(
                        ("accepted", "processing", "partially_filled", "unknown")
                    ),
                )
                .limit(1)
            )
            if pending is not None:
                raise ValueError("quant_order_pending")
            now = datetime.now(UTC)
            row = QuantOrderRow(
                id=uuid4(),
                account_id=account.id,
                instrument_id=instrument_id,
                service_id=service_id,
                scope_kind=source.scope_kind,
                target_id=source.target_id,
                sender_id=source.sender_id,
                message_id=source.message_id,
                event_time=command.event_timestamp,
                request_sha256=digest,
                mapping_version=command.mapping_version,
                settings_version=command.settings_version,
                commission_rate=command.settings.commission_rate,
                minimum_commission=command.settings.minimum_commission,
                stamp_tax_rate=command.settings.stamp_tax_rate,
                slippage=command.settings.slippage,
                provider=quote_provider or command.settings.provider,
                provider_order=list(command.settings.provider_order),
                quote_max_age_seconds=command.settings.max_quote_age_seconds,
                refresh_seconds=command.settings.refresh_seconds,
                quote_time=quote_time,
                quote_price=quote_price,
                side=command.action,
                quantity=quantity,
                status="accepted",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return _order(row), True
