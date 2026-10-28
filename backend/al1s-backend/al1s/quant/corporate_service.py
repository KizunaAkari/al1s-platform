"""Refresh corporate events and settle verified rights, one account transaction at a time."""

from datetime import UTC, date, datetime
from threading import Event

import structlog

from al1s.adapters.postgres.quant_accounts import Account
from al1s.adapters.postgres.quant_corporate_sync import QuantCorporateSync
from al1s.adapters.postgres.quant_entitlements import QuantEntitlements
from al1s.adapters.postgres.quant_instruments import Instrument
from al1s.quant.market_rules import CHINA
from al1s.quant.source_client import MarketSource


class CorporateService:
    def __init__(
        self,
        source: MarketSource,
        sync: QuantCorporateSync,
        rights: QuantEntitlements,
        *,
        now=lambda: datetime.now(UTC),
    ):
        self.source, self.sync, self.rights, self.now = source, sync, rights, now
        self.log = structlog.get_logger()
        self._stop = Event()

    def stop(self) -> None:
        self._stop.set()

    def _reconcile(self, instrument_id, since: date, today: date) -> None:
        for event_id in self.sync.events(instrument_id, since=since, today=today):
            try:
                self.rights.capture(event_id, today)
            except ValueError as error:
                self.log.warning("quant_corporate_capture_pending", error=str(error))
        cursor = None
        for _ in range(50):
            if self._stop.is_set():
                return
            batch = self.sync.due(instrument_id, today=today, after=cursor)
            if not batch:
                break
            for _day, right_id in batch:
                try:
                    self.rights.apply(right_id, today)
                except ValueError as error:
                    self.log.warning("quant_corporate_entitlement_pending", error=str(error))
            cursor = (batch[-1][0], batch[-1][1])
            if len(batch) < 100:
                break

    def ensure(self, instrument: Instrument, account: Account) -> None:
        if account.created_at is None:
            raise ValueError("quant_corporate_data_unavailable")
        created = (
            account.created_at
            if account.created_at.tzinfo
            else account.created_at.replace(tzinfo=UTC)
        )
        since = self.sync.first_account_date(instrument.id, created.astimezone(CHINA).date())
        now = self.now()
        try:
            self.sync.check_ready(instrument.id, account.id, since=since, now=now)
            return
        except ValueError as error:
            if str(error) == "quant_corporate_data_unavailable":
                rows = self.source.corporate(instrument.market.lower(), instrument.current_code)
                self.sync.save(instrument.id, rows, since=since)
            elif str(error) != "quant_corporate_needs_review":
                raise
        now = self.now()
        self._reconcile(instrument.id, since, now.astimezone(CHINA).date())
        self.sync.check_ready(instrument.id, account.id, since=since, now=self.now())

    def run_once(self) -> None:
        cursor = None
        for _ in range(100):
            if self._stop.is_set():
                return
            rows = self.sync.relevant(after=cursor, limit=100)
            if not rows:
                return
            for identity, market, code, created in rows:
                if self._stop.is_set():
                    return
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                since = created.astimezone(CHINA).date()
                try:
                    data = self.source.corporate(market.lower(), code)
                    self.sync.save(identity, data, since=since)
                    self._reconcile(identity, since, self.now().astimezone(CHINA).date())
                except (ValueError, OSError) as error:
                    self.log.warning(
                        "quant_corporate_source_unavailable", error=type(error).__name__
                    )
            cursor = rows[-1].id
            if len(rows) < 100:
                return
