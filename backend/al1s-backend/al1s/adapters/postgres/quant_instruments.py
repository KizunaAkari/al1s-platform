"""Stable security identities after an external source confirms the symbol."""

import re
from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.quant_models import QuantInstrumentRow
from al1s.quant.bse_mapping import code_for_date


@dataclass(frozen=True, slots=True)
class Instrument:
    id: UUID
    market: str
    current_code: str
    board: str


def infer_market(code: str) -> str:
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("quant_code_invalid")
    if code.startswith(("600", "601", "603", "605", "688")):
        return "SH"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZ"
    if code.startswith(("4", "8", "92")):
        return "BJ"
    raise ValueError("quant_code_invalid")


def classify(market: str, code: str, day: date) -> tuple[str, str]:
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) or type(day) is not date:
        raise ValueError("quant_code_invalid")
    if market == "BJ" and code.startswith(("4", "8", "92")):
        return code_for_date(code, day), "beijing"
    if market == "SH" and code.startswith("688"):
        return code, "star"
    if market == "SH" and code.startswith(("600", "601", "603", "605")):
        return code, "main"
    if market == "SZ" and code.startswith(("000", "001", "002", "003", "300", "301")):
        return code, "main"
    raise ValueError("quant_code_invalid")


class QuantInstruments:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def register(self, market: str, code: str, day: date) -> Instrument:
        current, board = classify(market, code, day)
        with self.sessions.begin() as session:
            session.execute(
                insert(QuantInstrumentRow)
                .values(
                    id=uuid4(),
                    market=market,
                    current_code=current,
                    board=board,
                    name=current,
                    active=True,
                )
                .on_conflict_do_nothing(
                    index_elements=[QuantInstrumentRow.market, QuantInstrumentRow.current_code]
                )
            )
            row = session.scalar(
                select(QuantInstrumentRow).where(
                    QuantInstrumentRow.market == market, QuantInstrumentRow.current_code == current
                )
            )
            if row is None or not row.active or row.board != board:
                raise ValueError("quant_instrument_unavailable")
            return Instrument(row.id, row.market, row.current_code, row.board)
