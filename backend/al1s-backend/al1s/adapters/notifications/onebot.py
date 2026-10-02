"""One HTTP attempt per delivery. Retry budgets belong to the dispatcher."""

import base64
import re
from collections.abc import Callable
from urllib.parse import urlsplit

from al1s.bots.runtime_connection import BotConnectionReader
from al1s.bots.types import BotServiceKind
from al1s.infrastructure.direct_http import DirectHttpError, post_json_direct
from al1s.notifications.errors import NotificationAdapterError
from al1s.notifications.types import AdapterReceipt, NotificationKind, NotificationMessage


def _post(
    url: str,
    token: str | None,
    payload: dict[str, object],
    *,
    auth_scheme: str = "Bearer",
    error_prefix: str = "onebot",
) -> object:
    try:
        return post_json_direct(url, token, payload, auth_scheme=auth_scheme)
    except DirectHttpError as exc:
        raise NotificationAdapterError(
            f"{error_prefix}_{exc.code}", retryable=exc.retryable
        ) from None


class OneBotNotificationAdapter:
    def __init__(
        self,
        connections: BotConnectionReader,
        post: Callable[[str, str | None, dict[str, object]], object] = _post,
    ):
        self._connections = connections
        self._post = post

    def send(self, message: NotificationMessage, *, idempotency_key: str) -> AdapterReceipt:
        if message.bot_service_id is None or len(message.targets) != 1:
            raise NotificationAdapterError("onebot_invalid_delivery", retryable=False)
        match = re.fullmatch(r"(private|group):([1-9][0-9]{0,15})", message.targets[0])
        if match is None or int(match[2]) > 9_007_199_254_740_991:
            raise NotificationAdapterError("onebot_invalid_target", retryable=False)
        connection = self._connections.read(message.bot_service_id, BotServiceKind.ONEBOT_GATEWAY)
        base = connection.settings.get("ONEBOT_BASE_URL")
        if not isinstance(base, str):
            raise NotificationAdapterError("onebot_invalid_endpoint", retryable=False)
        parsed = urlsplit(base)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise NotificationAdapterError("onebot_invalid_endpoint", retryable=False)
        action = "send_private_msg" if match[1] == "private" else "send_group_msg"
        target_key = "user_id" if match[1] == "private" else "group_id"
        segments = [{"type": "text", "data": {"text": message.body}}]
        if message.image_png is not None:
            if (
                message.kind is not NotificationKind.CONDITIONAL_SKIP
                or len(message.image_png) > 16 * 1024 * 1024
                or not message.image_png.startswith(b"\x89PNG\r\n\x1a\n")
            ):
                raise NotificationAdapterError("onebot_invalid_image", retryable=False)
            segments.append(
                {
                    "type": "image",
                    "data": {
                        "file": "base64://" + base64.b64encode(message.image_png).decode("ascii")
                    },
                }
            )
        result = self._post(
            base.rstrip("/") + "/" + action,
            connection.secret,
            {
                target_key: int(match[2]),
                "message": segments,
            },
        )
        # Do not propagate provider wording: it can echo message content or credentials.
        if (
            not isinstance(result, dict)
            or result.get("status") != "ok"
            or type(result.get("retcode")) is not int
            or result["retcode"] != 0
        ):
            raise NotificationAdapterError("onebot_action_rejected", retryable=False)
        data = result.get("data")
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if type(message_id) not in {int, str} or not re.fullmatch(r"-?[0-9]+", str(message_id)):
            raise NotificationAdapterError("onebot_receipt_invalid", retryable=False)
        return AdapterReceipt(provider_message_id=str(message_id))
