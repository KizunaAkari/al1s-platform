from decimal import Decimal

import pytest
from sqlalchemy import event

from al1s.adapters.postgres.trading_reply_repository import TradingReplyRepository
from al1s.quant.commands import DEFAULT_COMMANDS
from al1s.quant.contracts import QuantSettings
from tests.test_quant_settings import configured as configured


def test_single_command_save_is_atomic_idempotent_and_preserves_general_settings(configured):
    root, service = configured
    first = root.save(
        service,
        QuantSettings(initial_cash=123456),
        DEFAULT_COMMANDS,
        expected_version=0,
        operation_id="initial",
    )
    repo = TradingReplyRepository(root.sessions)
    repo.lock = lambda _session: None
    saved = repo.save(
        service,
        "quant",
        "buy",
        "买股票",
        {"main": "{stock_name} {quantity}股"},
        expected_version=first.row_version,
        operation_id="one",
    )
    assert saved.commands == {**DEFAULT_COMMANDS, "buy": "买股票"}
    assert saved.settings == first.settings and saved.mapping_version == 2
    assert saved.reply_templates == {"buy": {"main": "{stock_name} {quantity}股"}}
    assert (
        repo.save(
            service,
            "quant",
            "buy",
            "买股票",
            {"main": "{stock_name} {quantity}股"},
            expected_version=first.row_version,
            operation_id="one",
        )
        == saved
    )
    general = root.save(
        service,
        saved.settings.model_copy(update={"initial_cash": Decimal(222222)}),
        None,
        expected_version=saved.row_version,
        operation_id="general",
    )
    assert general.commands["buy"] == "买股票" and general.reply_templates == saved.reply_templates
    reset = repo.save(
        service,
        "quant",
        "buy",
        DEFAULT_COMMANDS["buy"],
        {},
        expected_version=general.row_version,
        operation_id="reset",
    )
    assert reset.reply_templates == {} and reset.commands == DEFAULT_COMMANDS


def test_conflicts_and_invalid_variables_do_not_partially_save(configured):
    root, service = configured
    first = root.save(
        service, QuantSettings(), DEFAULT_COMMANDS, expected_version=0, operation_id="initial"
    )
    repo = TradingReplyRepository(root.sessions)
    repo.lock = lambda _session: None
    for keyword, templates, expected in [
        (DEFAULT_COMMANDS["sell"], {"main": "文本"}, "conflict"),
        ("新买入", {"main": "{fake}"}, "variable"),
    ]:
        with pytest.raises(ValueError, match=expected):
            repo.save(
                service,
                "quant",
                "buy",
                keyword,
                templates,
                expected_version=first.row_version,
                operation_id=keyword,
            )
    assert root.get(service) == first


def test_template_snapshots_are_one_bounded_query_and_frozen_per_message(configured):
    root, service = configured
    repo = TradingReplyRepository(root.sessions)
    repo.lock = lambda _session: None
    saved = repo.save(
        service,
        "quant",
        "buy",
        "买股票",
        {"main": "{stock_name}"},
        expected_version=0,
        operation_id="initialize",
    )
    assert not saved.settings.enabled and len(saved.commands) == 8
    statements = []
    engine = root.sessions.kw["bind"]

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        snapshot = root.snapshots([service])[service]
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 4
    assert snapshot.reply_templates["buy"]["main"] == "{stock_name}"
