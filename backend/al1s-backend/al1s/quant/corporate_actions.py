"""Calculate verified record-date rights without inventing fractional entitlements."""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class CorporateEvent:
    record_date: date | None
    cash_date: date | None
    shares_date: date | None
    cash_per_share: Decimal
    shares_per_share: Decimal
    verified: bool


@dataclass(frozen=True, slots=True)
class Entitlement:
    eligible_quantity: int
    cash: Decimal
    shares: int


def parse_cninfo(row: dict[str, str]) -> CorporateEvent:
    def parsed_date(key: str) -> date | None:
        try:
            return date.fromisoformat(row.get(key, ""))
        except (ValueError, TypeError):
            return None

    description = row.get("实施方案分红说明", "")
    valid = bool(re.match(r"^(?:每)?10(?:股)?", description))
    ratios = []
    for field, pattern in (
        ("派息比例", r"派([0-9]+(?:\.[0-9]+)?)元"),
        ("送股比例", r"送([0-9]+(?:\.[0-9]+)?)股"),
        ("转增比例", r"转增([0-9]+(?:\.[0-9]+)?)股"),
    ):
        matches = re.findall(pattern, description)
        raw = row.get(field, "")
        if len(matches) > 1:
            valid = False
        try:
            value = Decimal(raw) if raw not in {"", "nan", "NaN", "None"} else Decimal(0)
            if (
                not value.is_finite()
                or value < 0
                or (value != (Decimal(matches[0]) if matches else 0))
            ):
                valid = False
                value = Decimal(0)
        except Exception:
            valid, value = False, Decimal(0)
        ratios.append(value / 10)
    record, cash_day, shares_day = (
        parsed_date("股权登记日"),
        parsed_date("派息日"),
        parsed_date("股份到账日"),
    )
    shares = ratios[1] + ratios[2]
    valid = bool(
        valid
        and record
        and (ratios[0] == 0 or (cash_day and cash_day >= record))
        and (shares == 0 or (shares_day and shares_day >= record))
        and (ratios[0] > 0 or shares > 0)
    )
    return CorporateEvent(record, cash_day, shares_day, ratios[0], shares, valid)


def entitlement(event: CorporateEvent, *, record_date_quantity: int) -> Entitlement:
    if (
        not event.verified
        or type(record_date_quantity) is not int
        or record_date_quantity < 0
        or not isinstance(event.record_date, date)
        or not event.cash_per_share.is_finite()
        or event.cash_per_share < 0
        or not event.shares_per_share.is_finite()
        or event.shares_per_share < 0
        or (
            event.cash_per_share > 0
            and (event.cash_date is None or event.cash_date < event.record_date)
        )
        or (
            event.shares_per_share > 0
            and (event.shares_date is None or event.shares_date < event.record_date)
        )
    ):
        raise ValueError("quant_corporate_needs_review")
    shares = event.shares_per_share * record_date_quantity
    cash = event.cash_per_share * record_date_quantity
    if shares != shares.to_integral_value() or cash != cash.quantize(Decimal("0.0001")):
        raise ValueError("quant_corporate_needs_review")
    return Entitlement(record_date_quantity, cash, int(shares))


def payable(
    event: CorporateEvent, right: Entitlement, today: date, *, cash_done: bool, shares_done: bool
) -> tuple[Decimal, int]:
    if not event.verified:
        raise ValueError("quant_corporate_needs_review")
    cash = (
        right.cash
        if not cash_done and event.cash_date is not None and today >= event.cash_date
        else Decimal(0)
    )
    shares = (
        right.shares
        if not shares_done and event.shares_date is not None and today >= event.shares_date
        else 0
    )
    return cash, shares
