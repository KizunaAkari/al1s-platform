import io
import json
import urllib.error
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.freqtrade.manager import NativeInstance
from al1s.freqtrade.service import FreqtradeService


def test_native_business_502_is_a_rejection_not_an_unknown_transport_failure(monkeypatch):
    instance = NativeInstance(MagicMock(), 19001, "test-password", 0, "0.001")
    body = json.dumps(
        {"error": "Error querying /api/v1/forceenter: Failed to enter position for BTC/USDT."}
    ).encode()
    opener = MagicMock()
    opener.open.side_effect = urllib.error.HTTPError(
        "http://127.0.0.1:19001/api/v1/forceenter", 502, "Bad Gateway", {}, io.BytesIO(body)
    )
    monkeypatch.setattr("urllib.request.build_opener", lambda *_: opener)
    with pytest.raises(ValueError, match="Failed to enter"):
        instance.call("forceenter", {"pair": "BTC/USDT"})


def test_missing_trade_id_cannot_be_reported_as_submitted():
    service = FreqtradeService(MagicMock(), MagicMock())
    native = MagicMock()
    native.call.side_effect = [[], {"status": "Error entering long trade for pair BTC/USDT."}]
    with pytest.raises(ValueError, match="未受理"):
        service.trade(
            native,
            SimpleNamespace(action="buy", arguments=("BTC", "100"), commands={"orders": "币成交"}),
            Decimal("0.001"),
            uuid4(),
        )
