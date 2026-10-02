import hashlib
import re
from collections.abc import Callable

from al1s.bots.runtime_connection import BotConnectionReader
from al1s.bots.types import BotServiceKind
from al1s.infrastructure.direct_http import DirectHttpError, post_json_direct
from al1s.notifications.errors import NotificationAdapterError
from al1s.notifications.types import AdapterReceipt, NotificationMessage


def _discord_post(url: str, token: str | None, payload: dict[str, object]) -> object:
    try:
        return post_json_direct(url, token, payload, auth_scheme="Bot")
    except DirectHttpError as exc:
        raise NotificationAdapterError(f"discord_{exc.code}", retryable=exc.retryable) from None


class DiscordNotificationAdapter:
    def __init__(
        self,
        connections: BotConnectionReader,
        post: Callable[[str, str | None, dict[str, object]], object] = _discord_post,
    ):
        self._connections = connections
        self._post = post

    def send(self, message: NotificationMessage, *, idempotency_key: str) -> AdapterReceipt:
        if message.bot_service_id is None or len(message.targets) != 1:
            raise NotificationAdapterError("discord_invalid_delivery", retryable=False)
        match = re.fullmatch(r"channel:([1-9][0-9]{16,19})", message.targets[0])
        if match is None or not 1 <= len(message.body) <= 2000:
            raise NotificationAdapterError("discord_invalid_message", retryable=False)
        connection = self._connections.read(message.bot_service_id, BotServiceKind.DISCORD_BRIDGE)
        if not connection.secret:
            raise NotificationAdapterError("discord_token_missing", retryable=False)
        result = self._post(
            f"https://discord.com/api/v10/channels/{match[1]}/messages",
            connection.secret,
            {
                "content": message.body,
                "allowed_mentions": {"parse": []},
                "nonce": hashlib.sha256(idempotency_key.encode()).hexdigest()[:24],
                "enforce_nonce": True,
            },
        )
        identity = result.get("id") if isinstance(result, dict) else None
        if not isinstance(identity, str) or not re.fullmatch(r"[1-9][0-9]{16,19}", identity):
            raise NotificationAdapterError("discord_receipt_invalid", retryable=False)
        return AdapterReceipt(provider_message_id=identity)
