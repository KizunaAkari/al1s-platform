"""Reconcile reported corporate actions without guessing conflicting events."""

import hashlib
import unicodedata
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.quant_models import (
    QuantAccountRow,
    QuantCorporateEventRow,
    QuantEntitlementRow,
    QuantFillRow,
    QuantInstrumentRow,
    QuantOrderRow,
)
from al1s.quant.corporate_actions import parse_cninfo
from al1s.quant.market_rules import CHINA


class QuantCorporateSync:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def relevant(self, *, after: UUID | None = None, limit: int = 100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("quant_pagination_invalid")
        with self.sessions() as session:
            query = (
                select(
                    QuantInstrumentRow.id,
                    QuantInstrumentRow.market,
                    QuantInstrumentRow.current_code,
                    func.min(QuantAccountRow.created_at),
                )
                .join(QuantOrderRow, QuantOrderRow.instrument_id == QuantInstrumentRow.id)
                .join(QuantAccountRow, QuantAccountRow.id == QuantOrderRow.account_id)
                .group_by(
                    QuantInstrumentRow.id,
                    QuantInstrumentRow.market,
                    QuantInstrumentRow.current_code,
                )
                .order_by(QuantInstrumentRow.id)
                .limit(limit)
            )
            if after is not None:
                query = query.where(QuantInstrumentRow.id > after)
            return session.execute(query).all()

    def events(self, instrument_id: UUID, *, since: date, today: date) -> list[UUID]:
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(QuantCorporateEventRow.id)
                    .where(
                        QuantCorporateEventRow.instrument_id == instrument_id,
                        QuantCorporateEventRow.state == "verified",
                        QuantCorporateEventRow.record_date >= since,
                        QuantCorporateEventRow.record_date < today,
                        QuantCorporateEventRow.captured_at.is_(None),
                    )
                    .order_by(QuantCorporateEventRow.record_date, QuantCorporateEventRow.id)
                    .limit(1000)
                ).all()
            )

    def first_account_date(self, instrument_id: UUID, fallback: date) -> date:
        with self.sessions() as session:
            created = session.scalar(
                select(func.min(QuantAccountRow.created_at))
                .join(QuantOrderRow, QuantOrderRow.account_id == QuantAccountRow.id)
                .where(QuantOrderRow.instrument_id == instrument_id)
            )
            if created is None:
                return fallback
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            return min(fallback, created.astimezone(CHINA).date())

    def due(self, instrument_id: UUID, *, today: date, after: tuple[date, UUID] | None = None):
        with self.sessions() as session:
            query = (
                select(QuantCorporateEventRow.record_date, QuantEntitlementRow.id)
                .join(
                    QuantCorporateEventRow,
                    QuantCorporateEventRow.id == QuantEntitlementRow.event_id,
                )
                .where(
                    QuantCorporateEventRow.instrument_id == instrument_id,
                    QuantCorporateEventRow.state == "verified",
                    QuantCorporateEventRow.record_date.is_not(None),
                    QuantEntitlementRow.state == "verified",
                    or_(
                        (QuantCorporateEventRow.cash_date <= today)
                        & (QuantCorporateEventRow.cash_per_share > 0)
                        & QuantEntitlementRow.cash_applied_at.is_(None),
                        (QuantCorporateEventRow.shares_date <= today)
                        & (QuantCorporateEventRow.shares_per_share > 0)
                        & QuantEntitlementRow.shares_applied_at.is_(None),
                    ),
                )
            )
            if after is not None:
                query = query.where(
                    or_(
                        QuantCorporateEventRow.record_date > after[0],
                        (QuantCorporateEventRow.record_date == after[0])
                        & (QuantEntitlementRow.id > after[1]),
                    )
                )
            return session.execute(
                query.order_by(QuantCorporateEventRow.record_date, QuantEntitlementRow.id).limit(
                    100
                )
            ).all()

    def save(
        self, instrument_id: UUID, rows: list[dict[str, str]], *, since: date | None = None
    ) -> None:
        if len(rows) > 1000:
            raise ValueError("quant_corporate_source_too_large")
        incoming = {}
        ambiguous = set()
        unidentified = set()
        for row in rows:
            value = parse_cninfo(row)
            if since and value.record_date and value.record_date < since:
                continue
            report = unicodedata.normalize("NFKC", row.get("报告时间", "")).strip()
            if (
                since
                and value.record_date is None
                and report[:4].isdigit()
                and int(report[:4]) < since.year - 1
            ):
                continue
            kind = unicodedata.normalize("NFKC", row.get("分红类型", "")).strip()
            source_id = f"{report}|{kind}"
            if not report or not kind or report in {"nan", "None"} or kind in {"nan", "None"}:
                source_id = "unverified|" + repr(sorted(row.items()))
            key = hashlib.sha256(source_id.encode()).hexdigest()
            if source_id.startswith("unverified|"):
                unidentified.add(key)
            if key in incoming and incoming[key] != value:
                ambiguous.add(key)
            incoming[key] = value
        with self.sessions.begin() as session:
            stock = session.get(QuantInstrumentRow, instrument_id, with_for_update=True)
            if stock is None:
                raise ValueError("quant_instrument_unavailable")
            existing = {
                row.canonical_event: row
                for row in session.scalars(
                    select(QuantCorporateEventRow)
                    .where(
                        QuantCorporateEventRow.instrument_id == instrument_id,
                        QuantCorporateEventRow.canonical_event.in_(list(incoming)),
                    )
                    .with_for_update()
                ).all()
            }
            conflicts = []
            for key, value in incoming.items():
                state = (
                    "verified"
                    if value.verified and key not in ambiguous and key not in unidentified
                    else "needs_review"
                )
                old = existing.get(key)
                if old is None:
                    session.add(
                        QuantCorporateEventRow(
                            id=uuid4(),
                            instrument_id=instrument_id,
                            canonical_event=key,
                            state=state,
                            record_date=value.record_date,
                            cash_date=value.cash_date,
                            shares_date=value.shares_date,
                            cash_per_share=value.cash_per_share,
                            shares_per_share=value.shares_per_share,
                            source="cninfo",
                        )
                    )
                elif (
                    old.source != "cninfo"
                    or old.record_date != value.record_date
                    or old.cash_date != value.cash_date
                    or old.shares_date != value.shares_date
                    or old.cash_per_share != value.cash_per_share
                    or old.shares_per_share != value.shares_per_share
                    or state != "verified"
                ):
                    old.state = "needs_review"
                    conflicts.append(old.id)
            if conflicts:
                affected = select(QuantEntitlementRow.account_id).where(
                    QuantEntitlementRow.event_id.in_(conflicts),
                    or_(
                        QuantEntitlementRow.cash_applied_at.is_not(None),
                        QuantEntitlementRow.shares_applied_at.is_not(None),
                    ),
                )
                session.execute(
                    update(QuantAccountRow)
                    .where(QuantAccountRow.id.in_(affected))
                    .values(state="needs_review", version=QuantAccountRow.version + 1)
                )
            stock.corporate_checked_at = datetime.now(UTC)

    def check_ready(
        self, instrument_id: UUID, account_id: UUID, *, since: date, now: datetime
    ) -> None:
        with self.sessions() as session:
            self.check_ready_in(session, instrument_id, account_id, since=since, now=now)

    @staticmethod
    def check_ready_in(
        session: Session, instrument_id: UUID, account_id: UUID, *, since: date, now: datetime
    ) -> None:
        if now.tzinfo is None:
            raise ValueError("quant_date_invalid")
        today = now.astimezone(CHINA).date()
        stock = session.get(QuantInstrumentRow, instrument_id)
        if stock is None or stock.corporate_checked_at is None:
            raise ValueError("quant_corporate_data_unavailable")
        checked = stock.corporate_checked_at
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        if checked > now or now - checked > timedelta(hours=1):
            raise ValueError("quant_corporate_data_unavailable")
        unknown_date = session.scalar(
            select(QuantCorporateEventRow.id)
            .where(
                QuantCorporateEventRow.instrument_id == instrument_id,
                QuantCorporateEventRow.state != "verified",
                QuantCorporateEventRow.record_date.is_(None),
            )
            .limit(1)
        )
        if unknown_date:
            raise ValueError("quant_corporate_needs_review")
        has_history = session.scalar(
            select(QuantFillRow.id)
            .join(QuantOrderRow, QuantOrderRow.id == QuantFillRow.order_id)
            .where(
                QuantOrderRow.account_id == account_id, QuantOrderRow.instrument_id == instrument_id
            )
            .limit(1)
        )
        if not has_history:
            return
        unresolved = session.scalar(
            select(QuantCorporateEventRow.id)
            .where(
                QuantCorporateEventRow.instrument_id == instrument_id,
                or_(
                    QuantCorporateEventRow.record_date.is_(None),
                    QuantCorporateEventRow.record_date >= since,
                ),
                or_(
                    (QuantCorporateEventRow.state != "verified")
                    & or_(
                        QuantCorporateEventRow.record_date.is_(None),
                        QuantCorporateEventRow.record_date <= today,
                    ),
                    (QuantCorporateEventRow.record_date < today)
                    & QuantCorporateEventRow.captured_at.is_(None),
                ),
            )
            .limit(1)
        )
        if unresolved:
            raise ValueError("quant_corporate_needs_review")
        pending = session.scalar(
            select(QuantEntitlementRow.id)
            .join(QuantCorporateEventRow, QuantCorporateEventRow.id == QuantEntitlementRow.event_id)
            .where(
                QuantEntitlementRow.account_id == account_id,
                QuantCorporateEventRow.instrument_id == instrument_id,
                or_(
                    (QuantCorporateEventRow.cash_date <= today)
                    & (QuantCorporateEventRow.cash_per_share > 0)
                    & QuantEntitlementRow.cash_applied_at.is_(None),
                    (QuantCorporateEventRow.shares_date <= today)
                    & (QuantCorporateEventRow.shares_per_share > 0)
                    & QuantEntitlementRow.shares_applied_at.is_(None),
                ),
            )
            .limit(1)
        )
        if pending:
            raise ValueError("quant_corporate_needs_review")
