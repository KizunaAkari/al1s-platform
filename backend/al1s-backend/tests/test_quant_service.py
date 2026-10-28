from decimal import Decimal
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.adapters.postgres.quant_accounts import Account, AccountSummary
from al1s.meme_workshop.contracts import CommandSource
from al1s.quant.commands import DEFAULT_COMMANDS, QuantCommand
from al1s.quant.contracts import QuantSettings
from al1s.quant.service import QuantService


def command(action, args=()):
    return QuantCommand(CommandSource(message_id="1", scope_kind="qq_group", target_id="123",
                                      sender_id="456"), action, args, 1, 1790000000,
                        settings=QuantSettings(initial_cash=Decimal("100000")),
                        command_words=tuple(DEFAULT_COMMANDS.items()))


def test_open_and_help_use_only_sender_and_current_command_words():
    account = Account(uuid4(), "456", Decimal("100000"), Decimal("100000"), "active", 1)
    accounts, orders = MagicMock(), MagicMock()
    accounts.open.return_value = (account, True)
    service = QuantService(accounts, orders)
    assert "100000" in service.execute(uuid4(), command("open"))
    accounts.open.assert_called_once_with("456", Decimal("100000"))
    assert "模拟买入" in service.execute(uuid4(), command("help"))
    assert "不接受" in service.execute(uuid4(), command("open", ("99999",)))


def test_asset_summary_does_not_claim_total_when_quotes_are_missing():
    account = Account(uuid4(), "456", Decimal("5000"), Decimal("100000"), "active", 1)
    accounts, orders = MagicMock(), MagicMock()
    accounts.summary.return_value = AccountSummary(account, 2, 1, None, None)
    result = QuantService(accounts, orders).execute(uuid4(), command("balance"))
    assert "5000" in result and "行情不完整" in result


def test_asset_summary_uses_complete_fresh_fallback_without_mixing_sources():
    account = Account(uuid4(), "456", Decimal("5000"), Decimal("100000"), "active", 1)
    accounts, orders = MagicMock(), MagicMock()
    fresh = datetime.now(UTC)
    accounts.summary.side_effect = [AccountSummary(account, 2, 2, Decimal("900"), fresh - timedelta(hours=1)),
                                    AccountSummary(account, 2, 2, Decimal("1000"), fresh)]
    result = QuantService(accounts, orders).execute(uuid4(), command("balance"))
    assert "腾讯" in result and "6000" in result and "已过期" not in result
    assert [call.kwargs["provider"] for call in accounts.summary.call_args_list] == ["sina", "tencent"]
