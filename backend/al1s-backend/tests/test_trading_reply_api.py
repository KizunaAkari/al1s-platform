from fastapi import FastAPI
from fastapi.testclient import TestClient

from al1s.adapters.postgres.trading_reply_repository import TradingReplyRepository
from al1s.api.trading_replies import router
from al1s.api.trading_replies import view
from al1s.freqtrade.contracts import FreqtradeRecord, FreqtradeSettings
from al1s.quant.gateway import QuantGateway
from tests.test_quant_settings import configured as configured


def test_futures_variables_use_futures_wallet_example_instead_of_spot_seed():
    from starlette.requests import Request
    from uuid import uuid4

    record = FreqtradeRecord(
        settings=FreqtradeSettings(initial_cash="1000000.0000", futures_initial_cash="1000.0000")
    )
    result = view(Request({"type": "http", "app": FastAPI()}), uuid4(), "freqtrade", record)
    opened = next(item for item in result["commands"] if item["id"] == "f_open")
    examples = {item["key"]: item["example"] for item in opened["variants"][0]["variables"]}
    assert examples["initial_cash"] == "1000"
    assert examples["market_label"] == "合约"
    assert examples["quote_currency"] == "USDT"


def test_single_command_api_and_example_preview_do_not_execute_transactions(configured):
    root, service = configured
    repo = TradingReplyRepository(root.sessions)
    repo.lock = lambda _session: None
    app = FastAPI()
    app.include_router(router)
    app.state.trading_replies = repo
    app.state.quant_settings = root
    app.state.quant_gateway = QuantGateway()
    app.state.quant_process = QuantGateway()
    client = TestClient(app)
    path = f"/bots/trading/commands/{service}/quant"
    listing = client.get(path)
    assert listing.status_code == 200 and len(listing.json()["commands"]) == 8
    buy = next(item for item in listing.json()["commands"] if item["id"] == "buy")
    examples = {item["key"]: item for item in buy["variants"][0]["variables"]}
    assert examples["market_label"]["example"] == "A股"
    assert examples["quote_currency"]["example"] == "元"
    assert examples["trigger"]["example"] == "模拟买入"
    assert examples["orders_command"]["label"] == "成交查询指令"
    preview = client.post(
        path + "/buy/preview",
        json={
            "keyword": "买股票",
            "templates": {"main": "{stock_name} {quantity}股"},
            "variant": "main",
        },
    )
    assert preview.status_code == 200 and preview.json()["content"] == "宁德时代 1000股"
    assert root.get(service).row_version == 0
    saved = client.put(
        path + "/buy",
        json={"keyword": "买股票", "templates": {"main": "{stock_name}"}},
        headers={"If-Match": "0", "Idempotency-Key": "one"},
    )
    assert saved.status_code == 200 and saved.json()["synchronized"] is True
    assert root.get(service).commands["buy"] == "买股票"
    assert (
        client.post(
            path + "/buy/preview",
            json={"keyword": "买股票", "templates": {"main": "{fake}"}, "variant": "main"},
        ).status_code
        == 422
    )
