import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.adapters.postgres.meme_settings import QqConnection
from al1s.adapters.postgres.quant_replies import Reply
from al1s.meme_workshop.contracts import CommandSource
from al1s.quant.commands import QuantCommand
from al1s.quant.contracts import QuantSettings
from al1s.quant.qq_dispatch import QuantQqDispatcher


def test_uncertain_command_and_send_are_not_retried():
    async def run():
        service = uuid4()
        row = QqConnection(service, uuid4(), 1, "ws://llbot:3001", "http://llbot:3000", 5, None)
        source = CommandSource(message_id="1", scope_kind="qq_group", target_id="123", sender_id="456")
        command = QuantCommand(source, "buy", ("600000", "100"), 1, 1790000000,
                               settings=QuantSettings(reply_interval_seconds=10))
        process, replies, client = MagicMock(), MagicMock(), MagicMock()
        process.execute.side_effect = TimeoutError()
        dispatcher = QuantQqDispatcher(process, replies)
        await dispatcher.dispatch(row, client, command)
        assert process.execute.call_count == 1
        assert "未确认" in replies.enqueue.call_args.args[2]
        assert replies.enqueue.call_args.args[3] == 10
        reply = Reply(uuid4(), service, source, "结果", "sending")
        replies.claim_next.return_value = reply
        client.send.side_effect = OSError()
        await dispatcher.send_once({service: client}, datetime.now(UTC))
        assert client.send.call_count == 1
        assert replies.settle.call_args.args[1] == "unknown"
    asyncio.run(run())
