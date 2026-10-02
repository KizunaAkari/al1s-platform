from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from al1s.bots.container_control import Alias, ContainerCommand, ContainerState, ControlError
from al1s.infrastructure.direct_http import NoRedirect


class ControlClient:
    def __init__(self, url: str, token: str):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"} or len(token) < 32):
            raise ControlError("bot_control_not_configured")
        self.url, self.token = url.rstrip("/"), token

    def call(self, alias: Alias, command: ContainerCommand | None = None) -> ContainerState:
        request = Request(
            f"{self.url}/containers/{alias}",
            data=command.model_dump_json().encode() if command else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST" if command else "GET",
        )
        try:
            # No redirects, ambient proxies or automatic retries of mutations.
            opener = build_opener(ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=80) as response:
                body = response.read(16_385)
                if len(body) > 16_384:
                    raise ControlError("invalid_control_response", 502)
                return ContainerState.model_validate(json.loads(body))
        except HTTPError as exc:
            exc.close()
            # Do not pass proxy bodies or credentials through the public API.
            code = "control_conflict_refresh_required" if exc.code == 409 else "control_rejected"
            raise ControlError(code, exc.code if exc.code in {404, 409} else 502) from exc
        except (OSError, URLError, ValueError) as exc:
            raise ControlError("control_unavailable_or_result_unknown") from exc
