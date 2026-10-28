from datetime import date
from decimal import Decimal

import pytest

from al1s.quant.corporate_actions import CorporateEvent, entitlement, payable, parse_cninfo


def test_record_date_rights_survive_subsequent_sale_and_apply_on_separate_dates():
    event = CorporateEvent(
        record_date=date(2026, 1, 5),
        cash_date=date(2026, 1, 8),
        shares_date=date(2026, 1, 10),
        cash_per_share=Decimal("0.5"),
        shares_per_share=Decimal("0.1"),
        verified=True,
    )
    right = entitlement(event, record_date_quantity=100)
    assert (right.eligible_quantity, right.cash, right.shares) == (100, Decimal("50.0000"), 10)
    assert payable(event, right, date(2026, 1, 8), cash_done=False, shares_done=False) == (
        Decimal("50.0000"),
        0,
    )
    assert payable(event, right, date(2026, 1, 10), cash_done=True, shares_done=False) == (
        Decimal("0"),
        10,
    )
    assert payable(event, right, date(2026, 1, 11), cash_done=True, shares_done=True) == (
        Decimal("0"),
        0,
    )


def test_uncertain_dates_and_fractional_shares_never_guess_rights():
    event = CorporateEvent(
        record_date=date(2026, 1, 5),
        cash_date=None,
        shares_date=None,
        cash_per_share=Decimal("0.5"),
        shares_per_share=Decimal("0.1"),
        verified=False,
    )
    with pytest.raises(ValueError, match="needs_review"):
        entitlement(event, record_date_quantity=100)
    fractional = CorporateEvent(
        record_date=date(2026, 1, 5),
        cash_date=None,
        shares_date=date(2026, 1, 10),
        cash_per_share=Decimal("0"),
        shares_per_share=Decimal("0.15"),
        verified=True,
    )
    with pytest.raises(ValueError, match="needs_review"):
        entitlement(fractional, record_date_quantity=11)


def test_cninfo_per_ten_ratios_and_required_dates():
    row = {"股权登记日": "2023-04-25", "派息日": "2023-04-26", "股份到账日": "2023-04-26",
           "派息比例": "25.2", "送股比例": "nan", "转增比例": "8.0",
           "实施方案分红说明": "10转增8股派25.2元(含税)"}
    event = parse_cninfo(row)
    assert event.verified and event.cash_per_share == Decimal("2.52") and event.shares_per_share == Decimal("0.8")
    row["股份到账日"] = "nan"
    assert not parse_cninfo(row).verified
