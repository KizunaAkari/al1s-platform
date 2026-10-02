"""Shared OneBot adapter fixture for notification unit tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.adapters.notifications.onebot import OneBotNotificationAdapter
from al1s.bots.runtime_connection import BotConnection


def setup_adapter(target: str = "private:123"):
    reader = MagicMock()
    reader.read.return_value = BotConnection({"ONEBOT_BASE_URL": "http://llbot:3000"}, "secret")
    post = MagicMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": -12}})
    message = SimpleNamespace(
        bot_service_id=uuid4(), targets=(target,), body="hello", image_png=None
    )
    return OneBotNotificationAdapter(reader, post), message, post
