"""Deduplicated, account-owned backtest result persistence."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.meme_base import fingerprint
from al1s.adapters.postgres.quant_models import QuantAccountRow, QuantBacktestRow
from al1s.quant.backtest_engine import BacktestResult
from al1s.quant.commands import QuantCommand


@dataclass(frozen=True, slots=True)
class BacktestRecord:
    id: UUID
    status: str
    total_return: Decimal | None
    max_drawdown: Decimal | None
    execution_count: int | None
    data_sha256: str | None
    provider: str | None = None


def _record(row: QuantBacktestRow) -> BacktestRecord:
    return BacktestRecord(
        row.id,
        row.status,
        row.total_return,
        row.max_drawdown,
        row.execution_count,
        row.data_sha256,
        row.provider,
    )


def _params(command: QuantCommand) -> dict:
    settings = command.settings
    if settings is None:
        raise ValueError("quant_backtest_invalid")
    return {
        "commission_rate": str(settings.commission_rate),
        "minimum_commission": str(settings.minimum_commission),
        "stamp_tax_rate": str(settings.stamp_tax_rate),
        "slippage": str(settings.slippage),
        "allocation": "0.75",
        "arguments": list(command.arguments),
    }


def _digest(
    command: QuantCommand,
    instrument_id: UUID,
    start: date,
    end: date,
    provider: str,
    adjustment: str,
    frequency: str,
) -> str:
    return fingerprint(
        {
            "action": command.action,
            "params": _params(command),
            "instrument": str(instrument_id),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "provider": provider,
            "adjustment": adjustment,
            "frequency": frequency,
            "mapping_version": command.mapping_version,
            "settings_version": command.settings_version,
        }
    )


class QuantBacktests:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def find(self, service_id: UUID, command: QuantCommand) -> BacktestRecord | None:
        source = command.source
        with self.sessions() as session:
            row = session.scalar(
                select(QuantBacktestRow).where(
                    QuantBacktestRow.service_id == service_id,
                    QuantBacktestRow.scope_kind == source.scope_kind,
                    QuantBacktestRow.target_id == source.target_id,
                    QuantBacktestRow.sender_id == source.sender_id,
                    QuantBacktestRow.message_id == source.message_id,
                    QuantBacktestRow.event_time == command.event_timestamp,
                )
            )
            if row is None:
                return None
            expected = _digest(
                command,
                row.instrument_id,
                row.start_date,
                row.end_date,
                row.provider,
                row.adjustment,
                row.frequency,
            )
            if (
                row.request_sha256 != expected
                or command.settings is None
                or row.initial_cash != command.settings.initial_cash
            ):
                raise ValueError("quant_event_conflict")
            return _record(row)

    def begin(
        self,
        service_id: UUID,
        command: QuantCommand,
        instrument_id: UUID,
        strategy: str,
        start: date,
        end: date,
        provider: str,
        adjustment: str,
        *,
        frequency: str = "1d",
    ) -> tuple[BacktestRecord, bool]:
        if command.action != "backtest" or command.settings is None or command.settings_version < 1:
            raise ValueError("quant_backtest_invalid")
        source, settings = command.source, command.settings
        params = _params(command)
        digest = _digest(command, instrument_id, start, end, provider, adjustment, frequency)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            account_id = session.scalar(
                select(QuantAccountRow.id).where(QuantAccountRow.qq_user_id == source.sender_id)
            )
            if account_id is None:
                raise ValueError("quant_account_not_found")
            stmt = insert(QuantBacktestRow).values(
                id=uuid4(),
                account_id=account_id,
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
                strategy=strategy,
                provider=provider,
                frequency=frequency,
                adjustment=adjustment,
                start_date=start,
                end_date=end,
                initial_cash=settings.initial_cash,
                parameters=params,
                status="processing",
                created_at=now,
                updated_at=now,
            )
            key = [
                QuantBacktestRow.service_id,
                QuantBacktestRow.scope_kind,
                QuantBacktestRow.target_id,
                QuantBacktestRow.sender_id,
                QuantBacktestRow.message_id,
                QuantBacktestRow.event_time,
            ]
            created = session.scalar(
                stmt.on_conflict_do_nothing(index_elements=key).returning(QuantBacktestRow.id)
            )
            row = session.scalar(
                select(QuantBacktestRow).where(
                    QuantBacktestRow.service_id == service_id,
                    QuantBacktestRow.scope_kind == source.scope_kind,
                    QuantBacktestRow.target_id == source.target_id,
                    QuantBacktestRow.sender_id == source.sender_id,
                    QuantBacktestRow.message_id == source.message_id,
                    QuantBacktestRow.event_time == command.event_timestamp,
                )
            )
            if row is None:
                raise RuntimeError("quant_backtest_unavailable")
            expected = _digest(
                command, instrument_id, start, end, row.provider, adjustment, frequency
            )
            if row.request_sha256 != expected or row.initial_cash != settings.initial_cash:
                raise ValueError("quant_event_conflict")
            return _record(row), created is not None

    def complete(self, identity: UUID, result: BacktestResult) -> None:
        with self.sessions.begin() as session:
            row = session.get(QuantBacktestRow, identity, with_for_update=True)
            if row is None or row.status != "processing":
                raise ValueError("quant_backtest_state_conflict")
            row.total_return = result.total_return.quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP
            )
            row.max_drawdown = result.max_drawdown.quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP
            )
            row.execution_count, row.data_sha256 = result.execution_count, result.data_sha256
            row.status, row.updated_at = "completed", datetime.now(UTC)

    def recover_interrupted(self) -> int:
        with self.sessions.begin() as session:
            result = session.execute(
                update(QuantBacktestRow)
                .where(QuantBacktestRow.status == "processing")
                .values(status="unknown", updated_at=datetime.now(UTC))
            )
            return result.rowcount

    def fail(self, identity: UUID, code: str) -> None:
        if code not in {
            "quant_backtest_failed",
            "quant_source_unavailable",
            "quant_market_data_missing",
        }:
            raise ValueError("quant_backtest_error_invalid")
        with self.sessions.begin() as session:
            row = session.get(QuantBacktestRow, identity, with_for_update=True)
            if row is None or row.status != "processing":
                raise ValueError("quant_backtest_state_conflict")
            row.status, row.error_code, row.updated_at = "failed", code, datetime.now(UTC)
