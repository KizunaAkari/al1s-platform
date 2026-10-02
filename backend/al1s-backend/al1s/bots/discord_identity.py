"""Resolve a Discord application's public ID from its Bot Token without storing it."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from al1s.bots.errors import BotDomainError
from al1s.infrastructure.direct_http import NoRedirect

_APPLICATION_URL = "https://discord.com/api/v10/oauth2/applications/@me"
_ID_PATTERN = re.compile(r"[0-9]{17,20}")
_MAX_RESPONSE_BYTES = 65_536


_NoRedirect = NoRedirect
def resolve_discord_application_id(token: str) -> str:
    secret = token.strip()
    if not 30 <= len(secret) <= 512 or any(ord(char) < 33 or ord(char) > 126 for char in secret):
        raise BotDomainError("invalid_discord_token", "请填写有效的 Discord Bot Token。")

    request = Request(
        _APPLICATION_URL,
        headers={
            "Authorization": f"Bot {secret}",
            "Accept": "application/json",
            "User-Agent": "AL1S/0.1",
        },
    )
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=6) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise BotDomainError(
                "discord_token_rejected", "Discord 未接受这个 Bot Token; 请检查后重试。", 422
            ) from None
        if exc.code == 429:
            raise BotDomainError(
                "discord_rate_limited", "Discord 暂时限制查询; 请稍后重试。", 429
            ) from None
        raise BotDomainError(
            "discord_lookup_failed", "暂时无法从 Discord 获取应用 ID; 请稍后重试。", 502
        ) from None
    except (URLError, TimeoutError, OSError):
        raise BotDomainError(
            "discord_lookup_unavailable", "暂时无法连接 Discord; 请稍后重试。", 503
        ) from None

    if len(raw) > _MAX_RESPONSE_BYTES:
        raise BotDomainError("discord_lookup_failed", "Discord 返回的应用信息无效。", 502)
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise BotDomainError("discord_lookup_failed", "Discord 返回的应用信息无效。", 502) from None
    application_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(application_id, str) or _ID_PATTERN.fullmatch(application_id) is None:
        raise BotDomainError("discord_lookup_failed", "Discord 返回的应用 ID 无效。", 502)
    return application_id
