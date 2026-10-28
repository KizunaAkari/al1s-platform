from datetime import datetime
from decimal import Decimal

import pytest

from al1s.quant.market_data import MarketBar
from al1s.quant.paper_engine import PaperInstrument, PaperRequest, validate_paper_request


def bar(time):
    return MarketBar(datetime.fromisoformat(time), Decimal("10"), Decimal("11"), Decimal("9"),
                     Decimal("10"), 10000)


def test_paper_uses_only_real_strictly_later_intraday_bars():
    request = PaperRequest(symbol="600000", side="buy", quantity=100,
                           requested_at=datetime.fromisoformat("2026-10-27T09:30:30+08:00"),
                           instruments=(PaperInstrument("600000", "main"),), cash=Decimal("100000"),
                           state=None, commission_rate=Decimal(0), minimum_commission=Decimal(0),
                           stamp_tax_rate=Decimal(0), slippage=Decimal(0),
                           expected_positions=(("600000", 0),), initial_cash=Decimal("100000"))
    later = [bar("2026-10-27T09:31:00+08:00"), bar("2026-10-27T09:32:00+08:00"),
             bar("2026-10-27T09:33:00+08:00")]
    assert validate_paper_request(request, later) == later
    with pytest.raises(ValueError, match="bar"):
        validate_paper_request(request, [bar("2026-10-27T09:30:00+08:00"), *later])
    with pytest.raises(ValueError, match="bar"):
        validate_paper_request(request, [later[0], later[0], later[1]])
