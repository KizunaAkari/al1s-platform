from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.adapters.postgres.quant_accounts import Account
from al1s.adapters.postgres.quant_instruments import Instrument
from al1s.quant.corporate_service import CorporateService


def test_missing_corporate_source_never_allows_order_to_proceed():
    source, sync, rights = MagicMock(), MagicMock(), MagicMock()
    sync.first_account_date.return_value = datetime(2026, 10, 1, tzinfo=UTC).date()
    sync.check_ready.side_effect = ValueError("quant_corporate_data_unavailable")
    source.corporate.side_effect = ValueError("quant_source_unavailable")
    account = Account(uuid4(), "123", Decimal("100000"), Decimal("100000"), "active", 1,
                      datetime(2026, 10, 1, tzinfo=UTC))
    instrument = Instrument(uuid4(), "SH", "600000", "main")
    manager = CorporateService(source, sync, rights)
    with pytest.raises(ValueError, match="source_unavailable"):
        manager.ensure(instrument, account)
    rights.apply.assert_not_called()


def test_due_rights_use_bounded_pages_even_if_one_account_is_pending():
    from datetime import date
    source, sync, rights = MagicMock(), MagicMock(), MagicMock()
    sync.events.return_value = []
    first = [(date(2026, 1, 5), uuid4()) for _ in range(100)]
    second = [(date(2026, 1, 6), uuid4())]
    sync.due.side_effect = [first, second]
    rights.apply.side_effect = [ValueError("quant_order_pending")] + [True] * 100
    CorporateService(source, sync, rights)._reconcile(uuid4(), date(2026, 1, 1), date(2026, 1, 8))
    assert rights.apply.call_count == 101
    assert sync.due.call_args.kwargs["after"] == first[-1]
